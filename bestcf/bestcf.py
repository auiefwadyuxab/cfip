#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BestCF link collector and four-panel synchronizer."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener

SOURCE_URL = (
    "https://raw.githubusercontent.com/wanwushequ/cfyxip/"
    "refs/heads/main/index.html"
)
TARGET_SECTIONS = {"yxym": "优选域名 BestDomain", "yxip": "优选IP BestIP"}
OUTPUT_NAME = "bestcf.txt"
LOG_NAME = "bestcf.log"
YUAN_NAME = "yuan.txt"
MAX_PANEL_LINKS = 49
FETCH_RETRIES = 4
HTTP_TIMEOUT = 30
PANEL_TIMEOUT = 25
USER_AGENT = "Mozilla/5.0 (compatible; BestCF-Collector/3.1)"
URL_PATTERN = re.compile(r"^(?:https?://|sub://)\S+$", re.IGNORECASE)
COPY_PATTERN = re.compile(r"copy\s*\(\s*this\s*,\s*(['\"])(.*?)\1\s*\)", re.I | re.S)
ADMIN_ENV = "ADMIN"
PANEL_URL_ENV = "url"


class BestCFError(RuntimeError):
    pass


class PanelReadError(BestCFError):
    def __init__(self, message: str, states: dict[int, "PanelState"]) -> None:
        super().__init__(message)
        self.states = dict(states)


class BestCFParser(HTMLParser):
    """Read actual URL values in the two wanted HTML sections."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.section_stack: list[str | None] = []
        self.fallback_key: str | None = None
        self.current_heading: list[str] = []
        self.in_h2 = False
        self.captures: list[dict[str, object]] = []
        self.links: dict[str, list[str]] = {"yxym": [], "yxip": []}
        self.found: set[str] = set()

    @property
    def current_key(self) -> str | None:
        for key in reversed(self.section_stack):
            if key:
                return key
        return self.fallback_key

    @staticmethod
    def normalize(value: str | None) -> str | None:
        if not value:
            return None
        value = " ".join(html.unescape(value).strip().split()).rstrip("，。；、")
        if not URL_PATTERN.fullmatch(value):
            return None
        parsed = urlsplit(value)
        if not parsed.netloc:
            return None
        return value

    @staticmethod
    def heading_key(heading: str) -> str | None:
        value = " ".join(heading.split()).strip().lower()
        if value == "优选域名 bestdomain":
            return "yxym"
        if value == "优选ip bestip":
            return "yxip"
        return None

    def add(self, key: str | None, value: str | None) -> None:
        if key not in self.links:
            return
        candidate = self.normalize(value)
        if candidate:
            self.links[key].append(candidate)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = dict(attrs)
        if tag == "section":
            section_id = (attr.get("id") or "").strip()
            key = section_id if section_id in TARGET_SECTIONS else None
            self.section_stack.append(key)
            if key:
                self.found.add(key)

        # Heading fallback must be active even when the source page no longer
        # wraps the target blocks in the expected section IDs.
        if tag == "h2":
            self.in_h2 = True
            self.current_heading = []

        key = self.current_key
        if not key:
            return

        for name in ("href", "data-url", "data-link"):
            self.add(key, attr.get(name))
        if tag == "option":
            # The value is the actual URL; visible option text may be an alias.
            self.add(key, attr.get("value"))
        for match in COPY_PATTERN.finditer(attr.get("onclick") or ""):
            self.add(key, match.group(2))

        classes = (attr.get("class") or "").split()
        if "url-text" in classes and tag not in ("input", "select", "textarea"):
            self.captures.append({"tag": tag, "key": key, "parts": []})

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self.in_h2:
            self.current_heading.append(data)
        for capture in self.captures:
            parts = capture.get("parts")
            if isinstance(parts, list):
                parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self.captures) - 1, -1, -1):
            capture = self.captures[index]
            if capture.get("tag") == tag:
                self.captures.pop(index)
                parts = capture.get("parts")
                key = capture.get("key")
                if isinstance(parts, list):
                    self.add(key if isinstance(key, str) else None, "".join(parts))
                break

        if tag == "h2" and self.in_h2:
            key = self.heading_key("".join(self.current_heading))
            # Every heading ends the previous fallback section. An unrelated
            # heading must not keep subsequent links attached to an old block.
            self.fallback_key = key
            if key:
                self.found.add(key)
            self.in_h2 = False
            self.current_heading = []

        if tag == "section" and self.section_stack:
            self.section_stack.pop()
            if not self.section_stack:
                self.fallback_key = None
                self.captures.clear()

    def unique_links(self) -> tuple[list[str], list[str]]:
        result: list[list[str]] = []
        for key in ("yxym", "yxip"):
            result.append(unique(self.links[key]))
        return result[0], result[1]


def unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        value = value.strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def same_link_set(actual: Iterable[str], target: Iterable[str]) -> bool:
    """Compare panel contents without treating order or repeated rows as changes."""
    return set(actual) == set(target)


def fresh_source_url(source_url: str) -> str:
    parsed = urlsplit(source_url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["_bestcf_ts"] = str(int(time.time()))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def fetch_html(source_url: str, retries: int = FETCH_RETRIES, timeout: int = HTTP_TIMEOUT) -> str:
    if source_url.startswith("file://"):
        from urllib.parse import unquote
        from urllib.request import url2pathname
        return Path(url2pathname(unquote(urlsplit(source_url).path))).read_text(encoding="utf-8")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
    }
    request_url = fresh_source_url(source_url)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            with build_opener().open(Request(request_url, headers=headers), timeout=timeout) as response:
                data = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return data.decode(charset, errors="replace")
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                delay = min(2 ** (attempt - 1), 8)
                print(f"  ! 抓取失败（{attempt}/{retries}），{delay} 秒后重试")
                time.sleep(delay)
    raise BestCFError(f"网页抓取失败，已重试 {retries} 次：{type(last_error).__name__}")


def extract_links(source_html: str) -> tuple[list[str], list[str]]:
    parser = BestCFParser()
    parser.feed(source_html)
    parser.close()
    missing = set(TARGET_SECTIONS) - parser.found
    if missing:
        names = "、".join(TARGET_SECTIONS[key] for key in sorted(missing))
        raise BestCFError(f"网页缺少目标版块：{names}")
    domains, ips = parser.unique_links()
    if not domains or not ips:
        raise BestCFError("优选域名或优选IP版块没有提取到有效链接")
    return domains, ips


def variant_rank(value: str) -> int | None:
    if value.lower() == "all":
        return 10**18
    return int(value) if value.isdigit() else None


def filter_largest_variants(links: list[str]) -> tuple[list[str], list[str]]:
    """Only collapse alternatives that are clearly the same URL family."""
    groups: dict[tuple[object, ...], list[tuple[int, str]]] = {}
    for link in links:
        parts = urlsplit(link)
        match = re.search(r"(?i)top(\d+)", parts.path)
        if match:
            family = parts.path[:match.start(1)] + "{N}" + parts.path[match.end(1):]
            key = ("top", parts.scheme.lower(), parts.netloc.lower(), family, parts.query, parts.fragment)
            groups.setdefault(key, []).append((int(match.group(1)), link))
            continue

        query = parse_qsl(parts.query, keep_blank_values=True)
        values = [value for name, value in query if name.lower() == "ips"]
        if len(values) == 1:
            rank = variant_rank(values[0])
            if rank is not None:
                rest = tuple((name, value) for name, value in query if name.lower() != "ips")
                key = ("ips", parts.scheme.lower(), parts.netloc.lower(), parts.path, rest, parts.fragment)
                groups.setdefault(key, []).append((rank, link))

    removed: set[str] = set()
    notes: list[str] = []
    for entries in groups.values():
        if len(entries) < 2:
            continue
        highest = max(rank for rank, _ in entries)
        lower = [link for rank, link in entries if rank < highest]
        removed.update(lower)
        if lower:
            shown = "all" if highest >= 10**18 else str(highest)
            notes.append(f"相同来源的数量档位保留最大值 {shown}，过滤较小档 {len(lower)} 条")
    return [link for link in links if link not in removed], notes


def read_existing_output(path: Path) -> tuple[list[str], list[str], str]:
    if not path.exists():
        return [], [], ""
    raw = path.read_text(encoding="utf-8-sig")
    subs: list[str] = []
    others: list[str] = []
    for line in raw.splitlines():
        value = line.strip()
        if not value:
            continue
        (subs if value.lower().startswith("sub://") else others).append(value)
    return unique(subs), unique(others), raw


def keep_old_order(old: list[str], new: list[str]) -> list[str]:
    new_set = set(new)
    kept = [value for value in old if value in new_set]
    kept_set = set(kept)
    return kept + [value for value in new if value not in kept_set]


def render_output(subs: list[str], others: list[str]) -> str:
    blocks: list[str] = []
    if subs:
        blocks.append("\n".join(subs))
    if others:
        blocks.append("\n".join(others))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except FileNotFoundError:
            pass
        raise


def read_yuan(path: Path) -> list[str]:
    if not path.is_file():
        raise BestCFError(f"找不到 {YUAN_NAME}，请把第二个面板的原始链接放在脚本同目录")
    links: list[str] = []
    invalid: list[int] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        parsed = urlsplit(value)
        if not URL_PATTERN.fullmatch(value) or parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            invalid.append(line_no)
        else:
            links.append(value)
    if invalid:
        raise BestCFError(f"yuan.txt 第 {', '.join(map(str, invalid))} 行格式不正确")
    result = unique(links)
    if not result:
        raise BestCFError("yuan.txt 没有有效链接，为避免误清空第二个面板，已停止")
    if len(result) > MAX_PANEL_LINKS:
        raise BestCFError(f"yuan.txt 去重后有 {len(result)} 条，超过单面板49条上限")
    return result


def build_targets(subs: list[str], others: list[str], yuan: list[str]) -> dict[int, list[str]]:
    """URL line 1=ordinary first 49; line 4=ordinary next 49;
    line 2=yuan + overflow; line 3=subscriptions.
    """
    first = others[:49]
    fourth = others[49:98]
    overflow = others[98:]
    targets = {
        1: unique(first),
        2: unique(yuan + overflow),
        3: unique(subs),
        4: unique(fourth),
    }
    for line_no, links in targets.items():
        if len(links) > MAX_PANEL_LINKS:
            raise BestCFError(
                f"url 第{line_no}行面板需要 {len(links)} 条，超过49条；本轮不会写入任何面板"
            )
    return targets


def parse_panel_text(body: str, line_no: int) -> list[str]:
    value = body.lstrip("\ufeff").strip()
    if not value or value.lower() == "null":
        return []
    prefix = value[:500].lower()
    if value.startswith(("{", "[")) or "<html" in prefix or "<!doctype" in prefix:
        raise BestCFError(f"url 第{line_no}行面板返回的不是纯文本，拒绝覆盖")
    return [line.strip() for line in value.splitlines() if line.strip()]


def read_panel_urls() -> list[str]:
    raw = os.environ.get(PANEL_URL_ENV, "")
    entries = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(entries) != 4:
        raise BestCFError("GitHub Secret `url` 必须是4行完整的 https://域名/admin 地址")
    hosts: list[str] = []
    for number, entry in enumerate(entries, start=1):
        parsed = urlsplit(entry)
        if (
            parsed.scheme.lower() != "https" or not parsed.netloc
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/admin"
        ):
            raise BestCFError(f"GitHub Secret `url` 第{number}行格式错误，应为完整的 https://域名/admin")
        hosts.append(urlunsplit((parsed.scheme, parsed.netloc, "", "", "")).rstrip("/"))
    if len(set(hosts)) != 4:
        raise BestCFError("GitHub Secret `url` 中有重复地址，请检查四行")
    return hosts


def read_admin_password() -> str:
    password = os.environ.get(ADMIN_ENV, "").strip()
    if not password:
        raise BestCFError("没有读取到 GitHub Secret `ADMIN`，四个面板共用同一个密码")
    return password


@dataclass
class PanelState:
    line_no: int
    old_links: list[str]
    target_links: list[str]
    changed: bool
    write_status: str = "待更新"
    write_error: str = ""


class PanelClient:
    def __init__(self, line_no: int, host: str, password: str) -> None:
        self.line_no = line_no
        self.host = host.rstrip("/")
        self.password = password
        self.cookie_jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookie_jar))
        self.logged_in = False

    def request(self, request: Request) -> tuple[int, str, str, str]:
        with self.opener.open(request, timeout=PANEL_TIMEOUT) as response:
            data = response.read()
            charset = response.headers.get_content_charset() or "utf-8"
            return response.status, data.decode(charset, errors="replace"), response.headers.get("Content-Type", ""), response.geturl()

    def login(self) -> None:
        body = urlencode({"password": self.password}).encode("utf-8")
        request = Request(
            self.host + "/login",
            data=body,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json,text/plain,*/*",
            },
            method="POST",
        )
        try:
            status, response_body, _, _ = self.request(request)
        except HTTPError as exc:
            raise BestCFError(f"url 第{self.line_no}行登录 HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BestCFError(f"url 第{self.line_no}行登录请求失败（{type(exc).__name__}）") from exc

        # EdgeTunnel may return HTTP 200 + Set-Cookie while the final URL
        # remains /login. Do NOT require a redirect to /admin here.
        if status != 200 or not list(self.cookie_jar):
            # CFData-WEB follows the same rule: login must return 200 and a cookie.
            raise BestCFError(f"url 第{self.line_no}行登录没有取得有效 Cookie；请检查 ADMIN 和地址")
        # Some versions return a JSON success flag; when JSON is present and
        # explicitly says failure, stop. Plain/empty responses remain allowed.
        try:
            payload = json.loads(response_body)
            if isinstance(payload, dict) and payload.get("success") is False:
                raise BestCFError(f"url 第{self.line_no}行登录返回失败")
        except json.JSONDecodeError:
            pass
        self.logged_in = True

    def read_add_txt(self) -> str:
        if not self.logged_in:
            self.login()
        endpoint = f"{self.host}/admin/ADD.txt?_t={int(time.time() * 1000)}"
        request = Request(endpoint, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/plain,*/*",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
        })
        try:
            status, body, content_type, final_url = self.request(request)
        except HTTPError as exc:
            raise BestCFError(f"url 第{self.line_no}行读取 HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BestCFError(f"url 第{self.line_no}行读取失败（{type(exc).__name__}）") from exc
        if status != 200 or urlsplit(final_url).path.rstrip("/") == "/login" or "text/html" in content_type.lower():
            raise BestCFError(f"url 第{self.line_no}行读取被带回登录页，Cookie 未通过验证")
        parse_panel_text(body, self.line_no)
        return body

    def write_add_txt(self, links: list[str]) -> None:
        if not self.logged_in:
            self.login()
        content = "\n".join(links) + ("\n" if links else "")
        request = Request(
            self.host + "/admin/ADD.txt",
            data=content.encode("utf-8"),
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "text/plain; charset=utf-8",
                "Accept": "application/json,text/plain,*/*",
            },
            method="POST",
        )
        try:
            status, response_body, _, _ = self.request(request)
        except HTTPError as exc:
            raise BestCFError(f"url 第{self.line_no}行写入 HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BestCFError(f"url 第{self.line_no}行写入失败（{type(exc).__name__}）") from exc
        if status != 200:
            raise BestCFError(f"url 第{self.line_no}行写入返回 HTTP {status}")
        try:
            result = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise BestCFError(f"url 第{self.line_no}行写入响应不是有效 JSON") from exc
        if not isinstance(result, dict) or result.get("success") is not True:
            raise BestCFError(f"url 第{self.line_no}行没有确认写入成功")

    def write_and_verify(self, links: list[str]) -> None:
        self.write_add_txt(links)
        actual = parse_panel_text(self.read_add_txt(), self.line_no)
        matches = same_link_set(actual, links)
        if not matches:
            raise BestCFError(
                f"url 第{self.line_no}行回读不一致：目标 {len(links)} 条，实际 {len(actual)} 条"
            )


def read_all_panels(targets: dict[int, list[str]], password: str) -> tuple[dict[int, PanelClient], dict[int, PanelState]]:
    hosts = read_panel_urls()
    clients: dict[int, PanelClient] = {}
    states: dict[int, PanelState] = {}
    # Read all four first. Do not write any panel if one read/login fails.
    for line_no, host in enumerate(hosts, start=1):
        client = PanelClient(line_no, host, password)
        try:
            body = client.read_add_txt()
            old = unique(parse_panel_text(body, line_no))
            target = unique(targets[line_no])
            # All four panels compare the actual de-duplicated URL set; ordering and
            # duplicate-row count alone do not cause a remote write.
            changed = not same_link_set(old, target)
            clients[line_no] = client
            states[line_no] = PanelState(line_no, old, target, changed)
        except Exception as exc:
            raise PanelReadError(
                f"url 第{line_no}行读取/登录失败：{type(exc).__name__}: {exc}", states
            ) from exc
    return clients, states


def panel_role(number: int) -> str:
    return {
        1: "普通链接优先",
        2: "yuan.txt + 溢出",
        3: "sub 订阅",
        4: "普通链接第二顺序",
    }[number]


def bjt_now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def set_diff(old: list[str], new: list[str]) -> tuple[int, int]:
    old_set, new_set = set(old), set(new)
    return len(new_set - old_set), len(old_set - new_set)


def log_needs_compaction(path: Path) -> bool:
    """Detect the previous verbose report format and replace it once."""
    if not path.is_file():
        return False
    try:
        old_report = path.read_text(encoding="utf-8-sig")
    except OSError:
        return True
    return bool(re.search(r"https?://|sub://", old_report, re.IGNORECASE))


def sanitize_error_message(message: str) -> str:
    """Keep the useful failure detail without copying credentials or URLs into logs."""
    message = re.sub(r"https?://[^\s'\"]+", "<URL已隐藏>", message, flags=re.IGNORECASE)
    for secret_name in ("ADMIN", "url"):
        secret = os.environ.get(secret_name, "").strip()
        if secret:
            message = message.replace(secret, "<已隐藏>")
    return message


def make_log(
    now: str,
    domain_count: int,
    ip_count: int,
    subs: list[str],
    others: list[str],
    old_subs: list[str],
    old_others: list[str],
    yuan: list[str],
    targets: dict[int, list[str]],
    states: dict[int, PanelState] | None,
    result: str,
) -> str:
    sub_added, sub_removed = set_diff(old_subs, subs)
    other_added, other_removed = set_diff(old_others, others)
    overflow = max(0, len(targets[2]) - len(yuan))
    lines = [
        "╔══════════════════════════════════════════════════════╗",
        "║              BESTCF · 本次运行摘要                  ║",
        "╠══════════════════════════════════════════════════════╣",
        f"时间：{now}（北京时间）",
        "",
        "【抓取统计】",
        f"优选域名版块：{domain_count} 条",
        f"优选IP版块：{ip_count} 条",
        f"订阅链接：{len(subs)} 条（新增 {sub_added} / 删除 {sub_removed}）",
        f"普通链接：{len(others)} 条（新增 {other_added} / 删除 {other_removed}）",
        f"yuan.txt：{len(yuan)} 条（已去重）",
        f"临时溢出：{overflow} 条",
        "",
        "【面板分配】",
        f"第1行：{len(targets[1])}/49 条",
        f"第4行：{len(targets[4])}/49 条",
        f"第2行：{len(yuan)} 条原始 + {overflow} 条溢出 = {len(targets[2])}/49 条",
        f"第3行：{len(targets[3])}/49 条",
    ]
    if states:
        lines += ["", "【面板核对】"]
        for number in (1, 4, 2, 3):
            state = states.get(number)
            if state is None:
                lines.append(f"第{number}行：未完成读取（前序面板读取/登录已失败）")
                continue
            added, removed = set_diff(state.old_links, state.target_links)
            status = state.write_status if state.changed else "已一致（忽略顺序与重复差异）"
            detail = f"；错误：{state.write_error}" if state.write_error else ""
            lines.append(
                f"第{number}行：当前唯一 {len(set(state.old_links))} → 目标唯一 {len(set(state.target_links))}；"
                f"新增 {added} / 删除 {removed}；{status}{detail}"
            )
    lines += ["", "【结果】", result, "╚══════════════════════════════════════════════════════╝", ""]
    return "\n".join(lines)


def write_github_summary(status: str, now: str, subs: int, others: int,
                          states: dict[int, PanelState] | None = None) -> None:
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not summary_path:
        return
    lines = [
        "## BestCF 运行结果", "",
        f"- 时间：{now}（北京时间）",
        f"- 状态：{status}",
        f"- sub 订阅：{subs} 条",
        f"- 普通链接：{others} 条",
    ]
    if states:
        lines += ["", "| url 行 | 用途 | 当前 | 目标 | 状态 |", "|---:|---|---:|---:|---|"]
        for number in (1, 4, 2, 3):
            state = states.get(number)
            if state is None:
                lines.append(f"| {number} | {panel_role(number)} | - | - | 未完成读取 |")
            else:
                lines.append(
                    f"| {number} | {panel_role(number)} | {len(state.old_links)} | {len(state.target_links)} | "
                    f"{state.write_status} |"
                )
    with open(summary_path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="抓取优选链接并按 url Secret 顺序同步四个面板")
    parser.add_argument("--source-url", default=os.environ.get("BESTCF_SOURCE_URL", SOURCE_URL))
    parser.add_argument("--source-file", type=Path, help="本地 HTML 测试文件")
    parser.add_argument("--output", type=Path, default=here / OUTPUT_NAME)
    parser.add_argument("--yuan", type=Path, default=here / YUAN_NAME)
    parser.add_argument("--log", type=Path, default=here / LOG_NAME)
    parser.add_argument("--skip-panels", action="store_true", help="只测试提取和本地输出，不连接面板")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    now = bjt_now()
    old_subs: list[str] = []
    old_others: list[str] = []
    old_raw = ""
    domains_count = ips_count = 0
    subs: list[str] = []
    others: list[str] = []
    yuan: list[str] = []
    targets: dict[int, list[str]] = {}
    states: dict[int, PanelState] = {}
    result = "未完成"
    output_changed = False
    source_changed = False
    compact_old_log = False
    current_stage = "读取现有 bestcf.txt / bestcf.log"

    print("\n╭─ BESTCF · 优选链接同步 ────────────────────────────────╮")
    print(f"│ 时间：{now}（北京时间）")
    print("│ 模式：正常同步（手动与定时执行相同）")
    print("╰───────────────────────────────────────────────────────╯")

    try:
        old_subs, old_others, old_raw = read_existing_output(args.output)
        compact_old_log = log_needs_compaction(args.log)

        current_stage = "抓取来源页面"
        print("\n[1/4] 抓取来源页面")
        source_url = args.source_file.resolve().as_uri() if args.source_file else args.source_url
        source_html = fetch_html(source_url)
        domains, ips = extract_links(source_html)
        domains_count, ips_count = len(domains), len(ips)
        discovered = unique(domains + ips)
        selected, selection_notes = filter_largest_variants(discovered)
        found_subs = [item for item in selected if item.lower().startswith("sub://")]
        found_others = [item for item in selected if not item.lower().startswith("sub://")]
        subs = keep_old_order(old_subs, found_subs)
        others = keep_old_order(old_others, found_others)
        source_changed = set(old_subs) != set(subs) or set(old_others) != set(others)
        output_content = render_output(subs, others)
        output_changed = old_raw != output_content or not args.output.exists()
        print(f"  ✓ 域名版块 {domains_count} 条；IP版块 {ips_count} 条")
        print(f"  ✓ 去重筛选后：sub {len(subs)} 条；普通链接 {len(others)} 条")
        for note in selection_notes:
            print(f"  · {note}")

        current_stage = "读取 yuan.txt 并计算目标"
        print("\n[2/4] 读取 yuan.txt 并计算目标")
        yuan = read_yuan(args.yuan)
        targets = build_targets(subs, others, yuan)
        overflow = max(0, len(targets[2]) - len(yuan))
        print(f"  ✓ yuan.txt：原始 {len(yuan)} 条（去重后）")
        print(f"  ✓ 普通链接溢出：{overflow} 条")
        print(f"  ✓ 目标数量：第1行 {len(targets[1])}、第4行 {len(targets[4])}、第2行 {len(targets[2])}、第3行 {len(targets[3])}")

        if args.skip_panels:
            if output_changed:
                atomic_write(args.output, output_content)
                print("  ✓ bestcf.txt 已更新")
            else:
                print("  = bestcf.txt 无变化")
            print("\n本地提取测试完成。")
            return 0

        current_stage = "登录并读取四个面板"
        print("\n[3/4] 登录并读取四个面板")
        password = read_admin_password()
        clients, states = read_all_panels(targets, password)
        changed_lines = [number for number, state in states.items() if state.changed]
        for state in states.values():
            state.write_status = "待更新" if state.changed else "已一致（忽略顺序与重复差异）"
        print("\n面板核对：")
        for number in (1, 4, 2, 3):
            state = states[number]
            status = "需要更新" if state.changed else "已一致"
            print(f"  第{number}行（{panel_role(number)}）：{len(state.old_links)} → {len(state.target_links)}/49，{status}")

        if not source_changed and not output_changed and not changed_lines and not compact_old_log:
            result = "无变化：链接集合和四个面板均一致；所有文件和面板保持原样。"
            print("\n✓ 没有变化，不写入面板，不更新 bestcf.txt 和 bestcf.log。")
            write_github_summary("无变化", now, len(subs), len(others), states)
            return 0

        current_stage = "写入有变化的面板并回读核验"
        print("\n[4/4] 写入有变化的面板并回读核验")
        updated: list[int] = []
        for number in (1, 4, 2, 3):
            state = states[number]
            if not state.changed:
                print(f"  = 第{number}行：跳过")
                continue
            print(f"  ↻ 第{number}行：写入 {len(state.target_links)} 条…")
            try:
                clients[number].write_and_verify(state.target_links)
            except Exception as panel_exc:
                state.write_status = "写入/回读失败"
                state.write_error = f"{type(panel_exc).__name__}: {panel_exc}"
                print(f"  ✗ 第{number}行：{state.write_status}；{state.write_error}", file=sys.stderr)
                raise
            else:
                state.write_status = "已写入并回读核验"
                updated.append(number)
                print(f"  ✓ 第{number}行：写入成功，回读核验通过")

        if output_changed:
            atomic_write(args.output, output_content)
            print("  ✓ bestcf.txt 已更新")
        else:
            print("  = bestcf.txt 无变化，不重写")

        if updated:
            result = "成功：所有需要更新的面板均已写入并通过回读核验。"
        elif output_changed:
            result = "成功：本地输出已更新，面板原本已一致。"
        elif compact_old_log:
            result = "成功：面板和链接均无变化；旧日志已转换为仅含数量的格式。"
        else:
            result = "成功：本地链接文件发生变化；面板原本已一致。"
        report = make_log(
            now, domains_count, ips_count, subs, others, old_subs, old_others,
            yuan, targets, states, result,
        )
        atomic_write(args.log, report)
        print(f"\n✓ 运行完成：更新面板 {', '.join(map(str, updated)) if updated else '无'}；日志只记录数量。")
        write_github_summary("同步成功", now, len(subs), len(others), states)
        return 0

    except Exception as exc:
        if isinstance(exc, PanelReadError):
            states = exc.states
        safe_error = sanitize_error_message(f"{type(exc).__name__}: {exc}")
        result = f"失败：{safe_error}"
        print(f"\n✗ BESTCF 执行失败：{safe_error}", file=sys.stderr)
        try:
            if domains_count or ips_count:
                if not targets:
                    targets = {1: others[:49], 2: unique(yuan + others[98:]),
                               3: subs, 4: others[49:98]}
                report = make_log(
                    now, domains_count, ips_count, subs, others, old_subs, old_others,
                    yuan, targets, states or None, result,
                )
                report = report.replace(
                    "【结果】",
                    f"【失败阶段】\n{current_stage}\n\n【结果】",
                    1,
                )
            else:
                report = "\n".join([
                    "BESTCF 本次运行失败",
                    f"时间：{now}（北京时间）",
                    f"失败阶段：{current_stage}",
                    f"已取得优选域名版块：{domains_count} 条",
                    f"已取得优选IP版块：{ips_count} 条",
                    f"错误：{safe_error}",
                    "未使用未验证的抓取结果覆盖 bestcf.txt。",
                    "",
                ])
            atomic_write(args.log, report)
            print(f"失败日志已覆盖写入：{args.log}", file=sys.stderr)
        except Exception as log_exc:
            print(f"失败日志写入也未成功：{type(log_exc).__name__}: {log_exc}", file=sys.stderr)
        write_github_summary("执行失败", now, len(subs), len(others), states or None)
        print("本次没有使用未验证的抓取结果覆盖 bestcf.txt。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
