#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""BestCF link collector and four-panel EdgeTunnel synchronizer.

All four private admin URLs are read from the multiline environment variable
`url`. Their order in that variable is the only panel identity mechanism.
The canonical content for the second URL is bestcf/yuan.txt; overflow links
are added after it and disappear automatically when no longer needed.
"""
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
TARGET_SECTIONS = {
    "yxym": "优选域名 BestDomain",
    "yxip": "优选IP BestIP",
}
OUTPUT_NAME = "bestcf.txt"
LOG_NAME = "bestcf.log"
YUAN_NAME = "yuan.txt"
MAX_PANEL_LINKS = 49
FETCH_RETRIES = 4
HTTP_TIMEOUT = 30
PANEL_TIMEOUT = 25
USER_AGENT = "Mozilla/5.0 (compatible; BestCF-Collector/3.0)"
URL_PATTERN = re.compile(r"^(?:https?://|sub://)\S+$", re.IGNORECASE)
URL_IN_COPY_PATTERN = re.compile(
    r"copy\s*\(\s*this\s*,\s*(['\"])(.*?)\1\s*\)",
    re.IGNORECASE | re.DOTALL,
)
ADMIN_ENV = "ADMIN"
PANEL_URL_ENV = "url"


class BestCFError(RuntimeError):
    """Expected operational error with a user-readable message."""


class BestCFParser(HTMLParser):
    """Extract actual URL values only inside the two intended page sections."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._section_stack: list[str | None] = []
        self._fallback_key: str | None = None
        self._heading_parts: list[str] = []
        self._in_h2 = False
        self._captures: list[dict[str, object]] = []
        self.section_links: dict[str, list[str]] = {"yxym": [], "yxip": []}
        self.found_sections: set[str] = set()

    @property
    def current_key(self) -> str | None:
        for key in reversed(self._section_stack):
            if key:
                return key
        return self._fallback_key

    @staticmethod
    def _normalize_candidate(value: str | None) -> str | None:
        if value is None:
            return None
        candidate = html.unescape(value).strip()
        candidate = " ".join(candidate.split()).rstrip("，。；、")
        if not candidate or not URL_PATTERN.fullmatch(candidate):
            return None
        parts = urlsplit(candidate)
        if parts.scheme.lower() in ("http", "https", "sub") and not parts.netloc:
            return None
        return candidate

    @staticmethod
    def _section_key_from_heading(heading: str) -> str | None:
        normalized = " ".join(heading.split()).strip().lower()
        if normalized == "优选域名 bestdomain":
            return "yxym"
        if normalized == "优选ip bestip":
            return "yxip"
        return None

    def _add_link(self, key: str | None, value: str | None) -> None:
        if key not in self.section_links:
            return
        candidate = self._normalize_candidate(value)
        if candidate:
            self.section_links[key].append(candidate)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = dict(attrs)
        if tag == "section":
            section_id = (attr.get("id") or "").strip()
            key = section_id if section_id in TARGET_SECTIONS else None
            self._section_stack.append(key)
            if key:
                self.found_sections.add(key)

        key = self.current_key
        if not key:
            return
        if tag == "h2":
            self._in_h2 = True
            self._heading_parts = []

        self._add_link(key, attr.get("href"))
        self._add_link(key, attr.get("data-url"))
        self._add_link(key, attr.get("data-link"))

        # Read option.value, not the visible option label.
        if tag == "option":
            self._add_link(key, attr.get("value"))

        for match in URL_IN_COPY_PATTERN.finditer(attr.get("onclick") or ""):
            self._add_link(key, match.group(2))

        classes = (attr.get("class") or "").split()
        if "url-text" in classes and tag not in ("input", "select", "textarea"):
            self._captures.append({"tag": tag, "key": key, "parts": []})

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._in_h2:
            self._heading_parts.append(data)
        for capture in self._captures:
            parts = capture.get("parts")
            if isinstance(parts, list):
                parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        for index in range(len(self._captures) - 1, -1, -1):
            if self._captures[index].get("tag") == tag:
                capture = self._captures.pop(index)
                parts = capture.get("parts")
                key = capture.get("key")
                if isinstance(parts, list):
                    self._add_link(key if isinstance(key, str) else None, "".join(parts))
                break

        if tag == "h2" and self._in_h2:
            key = self._section_key_from_heading("".join(self._heading_parts))
            if key:
                self._fallback_key = key
                self.found_sections.add(key)
            self._in_h2 = False
            self._heading_parts = []

        if tag == "section" and self._section_stack:
            self._section_stack.pop()
            if not self._section_stack:
                self._fallback_key = None
                self._captures = [item for item in self._captures if item.get("key")]

    def unique_links(self) -> tuple[list[str], list[str]]:
        result: list[list[str]] = []
        for key in ("yxym", "yxip"):
            seen: set[str] = set()
            links: list[str] = []
            for link in self.section_links[key]:
                if link not in seen:
                    seen.add(link)
                    links.append(link)
            result.append(links)
        return result[0], result[1]


def _unique(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        value = value.strip()
        if value and value not in seen:
            seen.add(value)
            result.append(value)
    return result


def build_fresh_url(source_url: str) -> str:
    parts = urlsplit(source_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["_bestcf_ts"] = str(int(time.time()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def fetch_html(source_url: str, retries: int = FETCH_RETRIES, timeout: int = HTTP_TIMEOUT) -> str:
    if source_url.startswith("file://"):
        from urllib.request import url2pathname
        from urllib.parse import unquote
        return Path(url2pathname(unquote(urlsplit(source_url).path))).read_text(encoding="utf-8")

    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
    }
    request_url = build_fresh_url(source_url)
    last_error: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            request = Request(request_url, headers=headers, method="GET")
            with build_opener().open(request, timeout=timeout) as response:
                body = response.read(8 * 1024 * 1024 + 1)
                if len(body) > 8 * 1024 * 1024:
                    raise BestCFError("来源网页超过 8 MiB，已停止解析")
                charset = response.headers.get_content_charset() or "utf-8"
                return body.decode(charset, errors="replace")
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                wait = min(2 ** (attempt - 1), 8)
                print(f"  ! 抓取暂时失败（{attempt}/{retries}），{wait} 秒后重试")
                time.sleep(wait)
    raise BestCFError(f"抓取失败，已重试 {retries} 次：{type(last_error).__name__}: {last_error}")


def extract_links(source_html: str) -> tuple[list[str], list[str]]:
    parser = BestCFParser()
    parser.feed(source_html)
    parser.close()
    missing = set(TARGET_SECTIONS) - parser.found_sections
    if missing:
        names = "、".join(TARGET_SECTIONS[key] for key in sorted(missing))
        raise BestCFError(f"网页缺少目标版块：{names}")
    domains, ips = parser.unique_links()
    if not domains:
        raise BestCFError("优选域名版块没有提取到有效链接")
    if not ips:
        raise BestCFError("优选IP版块没有提取到有效链接")
    return domains, ips


def _variant_rank(value: str) -> int | None:
    if value.lower() == "all":
        return 10**18
    if value.isdigit():
        return int(value)
    return None


def select_highest_variants(links: list[str]) -> tuple[list[str], list[str]]:
    """Keep only the largest clearly equivalent numeric variant."""
    groups: dict[tuple[object, ...], list[tuple[int, str]]] = {}
    for link in links:
        parts = urlsplit(link)
        match = re.search(r"(?i)top(\d+)", parts.path)
        if match:
            family_path = parts.path[:match.start(1)] + "{N}" + parts.path[match.end(1):]
            key = ("top", parts.scheme.lower(), parts.netloc.lower(), family_path,
                   parts.query, parts.fragment)
            groups.setdefault(key, []).append((int(match.group(1)), link))
            continue

        query_items = parse_qsl(parts.query, keep_blank_values=True)
        values = [value for name, value in query_items if name.lower() == "ips"]
        if len(values) == 1:
            rank = _variant_rank(values[0])
            if rank is not None:
                other_query = tuple((name, value) for name, value in query_items if name.lower() != "ips")
                key = ("ips", parts.scheme.lower(), parts.netloc.lower(), parts.path,
                       other_query, parts.fragment)
                groups.setdefault(key, []).append((rank, link))

    excluded: set[str] = set()
    notes: list[str] = []
    for key, entries in groups.items():
        if len(entries) < 2:
            continue
        largest = max(rank for rank, _ in entries)
        removed = [link for rank, link in entries if rank < largest]
        excluded.update(removed)
        if removed:
            shown = "all" if largest >= 10**18 else str(largest)
            notes.append(f"同组数量档位保留最大值 {shown}，过滤较小档 {len(removed)} 条")
    return [link for link in links if link not in excluded], notes


def parse_existing_output(path: Path) -> tuple[list[str], list[str], str]:
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
    return _unique(subs), _unique(others), raw


def preserve_existing_order(old: list[str], discovered: list[str]) -> list[str]:
    current = set(discovered)
    kept = [value for value in old if value in current]
    kept_set = set(kept)
    return kept + [value for value in discovered if value not in kept_set]


def render_bestcf(sub_links: list[str], other_links: list[str]) -> str:
    blocks: list[str] = []
    if sub_links:
        blocks.append("\n".join(sub_links))
    if other_links:
        blocks.append("\n".join(other_links))
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def sha256_links(links: list[str]) -> str:
    payload = "\n".join(sorted(set(links)))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def read_yuan_file(path: Path) -> list[str]:
    if not path.exists():
        raise BestCFError(f"找不到 {YUAN_NAME}：请把原本第二个面板要保留的链接放在这个文件里")
    links: list[str] = []
    invalid: list[int] = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        value = raw.strip()
        if not value or value.startswith("#"):
            continue
        if not URL_PATTERN.fullmatch(value) or not value.lower().startswith(("http://", "https://")):
            invalid.append(line_no)
            continue
        parts = urlsplit(value)
        if not parts.netloc:
            invalid.append(line_no)
            continue
        links.append(value)
    if invalid:
        raise BestCFError(f"{YUAN_NAME} 第 {', '.join(map(str, invalid))} 行不是有效的 http/https 链接")
    unique = _unique(links)
    if not unique:
        raise BestCFError(f"{YUAN_NAME} 没有有效链接；为避免误清空原始内容，已停止运行")
    if len(unique) > MAX_PANEL_LINKS:
        raise BestCFError(f"{YUAN_NAME} 有 {len(unique)} 条，超过单面板 {MAX_PANEL_LINKS} 条上限")
    return unique


def build_panel_targets(
    sub_links: list[str], other_links: list[str], yuan_links: list[str]
) -> dict[int, list[str]]:
    """Build targets solely by secret URL line number.

    URL line 1: ordinary links 1-49
    URL line 2: yuan.txt + ordinary links after the first 98
    URL line 3: sub:// links
    URL line 4: ordinary links 50-98
    """
    first = other_links[:MAX_PANEL_LINKS]
    fourth = other_links[MAX_PANEL_LINKS:MAX_PANEL_LINKS * 2]
    overflow = other_links[MAX_PANEL_LINKS * 2:]
    second = _unique(yuan_links + overflow)
    third = _unique(sub_links)
    targets = {1: _unique(first), 2: second, 3: third, 4: _unique(fourth)}
    for line_no, links in targets.items():
        if len(links) > MAX_PANEL_LINKS:
            raise BestCFError(
                f"第 {line_no} 行面板需要 {len(links)} 条，超过 49 条上限；"
                "本轮不会写入任何面板。请减少来源链接或 yuan.txt 内容。"
            )
    return targets


def parse_panel_body(body: str, line_no: int) -> list[str]:
    text = body.lstrip("\ufeff").strip()
    if not text or text.lower() == "null":
        return []
    start = text[:500].lower()
    if text.startswith(("{", "[")) or "<html" in start or "<!doctype" in start:
        raise BestCFError(f"第 {line_no} 行面板返回的不是纯文本，拒绝覆盖")
    return [line.strip() for line in text.splitlines() if line.strip()]


def resolve_panel_urls() -> list[str]:
    raw = os.environ.get(PANEL_URL_ENV, "")
    values = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(values) != 4:
        raise BestCFError(
            "没有正确读取 GitHub Secret：url。请按顺序放入 4 行完整的 https://.../admin 地址，"
            "不要添加名称或其他文字。"
        )
    origins: list[str] = []
    for line_no, value in enumerate(values, start=1):
        parts = urlsplit(value)
        if (
            parts.scheme.lower() != "https" or not parts.netloc
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment
            or parts.path.rstrip("/") != "/admin"
        ):
            raise BestCFError(
                f"GitHub Secret：url 第 {line_no} 行格式不正确；必须是完整的 https://.../admin 地址"
            )
        origins.append(urlunsplit((parts.scheme, parts.netloc, "", "", "")).rstrip("/"))
    if len(set(origins)) != 4:
        raise BestCFError("GitHub Secret：url 中存在重复地址，请检查四行是否分别对应四个面板")
    return origins


def resolve_admin_password() -> str:
    password = os.environ.get(ADMIN_ENV, "").strip()
    if not password:
        raise BestCFError(
            "没有找到 GitHub Secret：ADMIN。四个面板共用同一个管理员密码，本轮没有写入面板。"
        )
    return password


@dataclass
class PanelSnapshot:
    line_no: int
    old_body: str
    old_links: list[str]
    target_links: list[str]
    changed: bool


class EdgeTunnelClient:
    def __init__(self, line_no: int, base_url: str, password: str) -> None:
        self.line_no = line_no
        self.base_url = base_url.rstrip("/")
        self.password = password
        self.cookie_jar = CookieJar()
        self.opener = build_opener(HTTPCookieProcessor(self.cookie_jar))
        self.logged_in = False

    def _request(self, request: Request) -> tuple[int, str, str, str]:
        with self.opener.open(request, timeout=PANEL_TIMEOUT) as response:
            data = response.read(1 * 1024 * 1024 + 1)
            if len(data) > 1 * 1024 * 1024:
                raise BestCFError(f"第 {self.line_no} 行面板响应超过 1 MiB，停止处理")
            charset = response.headers.get_content_charset() or "utf-8"
            return response.status, data.decode(charset, errors="replace"), response.headers.get("Content-Type", ""), response.geturl()

    def login(self) -> None:
        body = urlencode({"password": self.password}).encode("utf-8")
        request = Request(
            self.base_url + "/login", data=body,
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json,text/plain,*/*",
            },
            method="POST",
        )
        try:
            status, _, _, final_url = self._request(request)
        except HTTPError as exc:
            raise BestCFError(f"第 {self.line_no} 行面板登录 HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BestCFError(f"第 {self.line_no} 行面板登录请求失败（{type(exc).__name__}）") from exc
        if status != 200 or not list(self.cookie_jar):
            raise BestCFError(
                f"第 {self.line_no} 行面板登录失败；请检查 ADMIN 密码和后台地址"
            )
        if "/login" in urlsplit(final_url).path and "admin" not in urlsplit(final_url).path:
            raise BestCFError(f"第 {self.line_no} 行面板登录后仍停留在登录页")
        self.logged_in = True

    def read_add_txt(self) -> str:
        if not self.logged_in:
            self.login()
        endpoint = f"{self.base_url}/admin/ADD.txt?_bestcf_ts={int(time.time() * 1000)}"
        request = Request(
            endpoint,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/plain,*/*",
                "Cache-Control": "no-cache, no-store, must-revalidate",
                "Pragma": "no-cache",
            },
            method="GET",
        )
        try:
            status, body, content_type, final_url = self._request(request)
        except HTTPError as exc:
            raise BestCFError(f"第 {self.line_no} 行面板读取 HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BestCFError(f"第 {self.line_no} 行面板读取失败（{type(exc).__name__}）") from exc
        if status != 200 or "/login" in urlsplit(final_url).path or "text/html" in content_type.lower():
            raise BestCFError(f"第 {self.line_no} 行面板读取被重定向或返回登录页，拒绝覆盖")
        parse_panel_body(body, self.line_no)
        return body

    def write_add_txt(self, links: list[str]) -> None:
        if not self.logged_in:
            self.login()
        content = "\n".join(links) + ("\n" if links else "")
        request = Request(
            self.base_url + "/admin/ADD.txt",
            data=content.encode("utf-8"),
            headers={
                "User-Agent": USER_AGENT,
                "Content-Type": "text/plain; charset=utf-8",
                "Accept": "application/json,text/plain,*/*",
            },
            method="POST",
        )
        try:
            status, response_body, _, _ = self._request(request)
        except HTTPError as exc:
            raise BestCFError(f"第 {self.line_no} 行面板写入 HTTP {exc.code}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise BestCFError(f"第 {self.line_no} 行面板写入失败（{type(exc).__name__}）") from exc
        if status != 200:
            raise BestCFError(f"第 {self.line_no} 行面板写入返回 HTTP {status}")
        try:
            result = json.loads(response_body)
        except json.JSONDecodeError as exc:
            raise BestCFError(f"第 {self.line_no} 行面板写入结果不是有效 JSON") from exc
        if not isinstance(result, dict) or result.get("success") is not True:
            raise BestCFError(f"第 {self.line_no} 行面板没有确认写入成功")

    def write_and_verify(self, links: list[str]) -> None:
        self.write_add_txt(links)
        actual = parse_panel_body(self.read_add_txt(), self.line_no)
        matched = actual == links if self.line_no == 2 else (
            set(actual) == set(links) and len(actual) == len(links)
        )
        if not matched:
            raise BestCFError(
                f"第 {self.line_no} 行面板回读不一致：预期 {len(links)} 条，实际 {len(actual)} 条"
            )


def read_all_panels(
    targets: dict[int, list[str]], password: str
) -> tuple[dict[int, EdgeTunnelClient], dict[int, PanelSnapshot]]:
    urls = resolve_panel_urls()
    clients: dict[int, EdgeTunnelClient] = {}
    snapshots: dict[int, PanelSnapshot] = {}
    # Read all four before any write. This is a comparison, not a disk backup.
    for line_no, base_url in enumerate(urls, start=1):
        client = EdgeTunnelClient(line_no, base_url, password)
        old_body = client.read_add_txt()
        old_links = parse_panel_body(old_body, line_no)
        target = targets[line_no]
        # The second panel follows yuan.txt exactly, including order.
        # Other panels ignore order-only differences from upstream sorting.
        if line_no == 2:
            changed = old_links != target
        else:
            changed = set(old_links) != set(target) or len(old_links) != len(target)
        clients[line_no] = client
        snapshots[line_no] = PanelSnapshot(line_no, old_body, old_links, target, changed)
    return clients, snapshots


def _panel_role(line_no: int) -> str:
    return {
        1: "普通链接优先区",
        2: "yuan.txt 原始内容 + 临时溢出区",
        3: "sub 订阅专用区",
        4: "普通链接第二优先区",
    }[line_no]


def panel_delta(snapshot: PanelSnapshot) -> tuple[list[str], list[str]]:
    before, after = set(snapshot.old_links), set(snapshot.target_links)
    added = [value for value in snapshot.target_links if value not in before]
    removed = [value for value in snapshot.old_links if value not in after]
    return added, removed


def print_panel_overview(snapshots: dict[int, PanelSnapshot], yuan_count: int, overflow_count: int) -> None:
    print("\n┌─ 面板分配预览 ─────────────────────────────────────────────┐")
    for line_no in (1, 4, 2, 3):
        snap = snapshots[line_no]
        added, removed = panel_delta(snap)
        status = "需要更新" if snap.changed else "已一致"
        extra = ""
        if line_no == 2:
            extra = f"（原始 {yuan_count} + 溢出 {overflow_count}）"
        print(
            f"│ 第{line_no}行  {_panel_role(line_no):<28} "
            f"{len(snap.old_links):>2} → {len(snap.target_links):>2}/49  "
            f"+{len(added):<2} -{len(removed):<2}  {status}{extra}"
        )
    print("└────────────────────────────────────────────────────────────┘")
    for line_no in (1, 4, 2, 3):
        snap = snapshots[line_no]
        added, removed = panel_delta(snap)
        if added or removed:
            print(f"\n  第{line_no}行变化：")
            for link in added:
                print(f"    + {link}")
            for link in removed:
                print(f"    - {link}")


def bjt_now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def make_report(
    now: str,
    domain_count: int,
    ip_count: int,
    subs: list[str],
    others: list[str],
    yuan_links: list[str],
    targets: dict[int, list[str]],
    snapshots: dict[int, PanelSnapshot] | None,
    outcome: str,
    notes: list[str] | None = None,
) -> str:
    overflow_count = max(0, len(targets[2]) - len(yuan_links))
    lines = [
        "╔══════════════════════════════════════════════════════════════╗",
        "║                 BESTCF · 本轮运行报告                       ║",
        "╠══════════════════════════════════════════════════════════════╣",
        f"║ 时间：{now}（北京时间）",
        f"║ 来源：{SOURCE_URL}",
        "╠══════════════════════════════════════════════════════════════╣",
        "【一、抓取结果】",
        f"优选域名：{domain_count} 条",
        f"优选IP版块：{ip_count} 条",
        f"sub 订阅：{len(subs)} 条",
        f"普通链接：{len(others)} 条",
        f"yuan.txt 原始链接：{len(yuan_links)} 条",
        f"临时溢出：{overflow_count} 条",
        "",
        "【二、面板目标分配】",
        f"第1行：{len(targets[1])} 条（普通链接优先区）",
        f"第4行：{len(targets[4])} 条（普通链接第二优先区）",
        f"第2行：{len(yuan_links)} 条原始内容 + {overflow_count} 条临时溢出 = {len(targets[2])} 条",
        f"第3行：{len(targets[3])} 条（仅 sub）",
        "",
        "【三、sub 订阅链接】",
        *(subs or ["（无）"]),
        "",
        "【四、普通优选链接】",
        *(others or ["（无）"]),
        "",
        "【五、yuan.txt 原始内容】",
        *yuan_links,
        "",
    ]
    if notes:
        lines += ["【六、提取规则提示】", *[f"- {note}" for note in notes], ""]
    if snapshots:
        lines += ["【面板核对结果】"]
        for line_no in (1, 4, 2, 3):
            snap = snapshots[line_no]
            added, removed = panel_delta(snap)
            lines.append(
                f"第{line_no}行：当前 {len(snap.old_links)} 条 → 目标 {len(snap.target_links)} 条；"
                f"新增 {len(added)}，移除 {len(removed)}；"
                f"{'待更新' if snap.changed else '无需修改'}"
            )
    lines += ["", "【执行结果】", outcome, "", "╚══════════════════════════════════════════════════════════════╝", ""]
    return "\n".join(lines)


def write_github_summary(
    status: str,
    now: str,
    subs_count: int,
    others_count: int,
    snapshots: dict[int, PanelSnapshot] | None = None,
) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY", "").strip()
    if not path:
        return
    lines = [
        "## BestCF 同步结果", "",
        f"- 时间：{now}（北京时间）",
        f"- 结果：{status}",
        f"- sub 订阅：{subs_count} 条",
        f"- 普通链接：{others_count} 条",
    ]
    if snapshots:
        lines += ["", "| URL 行号 | 用途 | 当前 | 目标 | 状态 |", "|---:|---|---:|---:|---|"]
        for line_no in (1, 4, 2, 3):
            snap = snapshots[line_no]
            lines.append(
                f"| {line_no} | {_panel_role(line_no)} | {len(snap.old_links)} | "
                f"{len(snap.target_links)} | {'待更新' if snap.changed else '一致'} |"
            )
    lines.append("")
    with open(path, "a", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")


def parse_args() -> argparse.Namespace:
    here = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="提取 BestCF 链接并按 url Secret 的行顺序同步四个面板")
    parser.add_argument("--source-url", default=os.environ.get("BESTCF_SOURCE_URL", SOURCE_URL))
    parser.add_argument("--source-file", type=Path, help="本地 HTML 测试文件")
    parser.add_argument("--output", type=Path, default=here / OUTPUT_NAME)
    parser.add_argument("--yuan", type=Path, default=here / YUAN_NAME)
    parser.add_argument("--log", type=Path, default=here / LOG_NAME)
    parser.add_argument("--skip-panels", action="store_true", help="仅测试网页提取，不连接面板")
    dry_default = os.environ.get("BESTCF_DRY_RUN", "false").strip().lower() in {"1", "true", "yes", "on"}
    parser.add_argument("--dry-run", action="store_true", default=dry_default,
                        help="只读取和预览，不修改文件或面板")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    old_subs, old_others, old_raw = parse_existing_output(args.output)
    now = bjt_now()
    domain_count = ip_count = 0
    subs: list[str] = []
    others: list[str] = []
    yuan_links: list[str] = []
    targets: dict[int, list[str]] = {}
    snapshots: dict[int, PanelSnapshot] = {}

    print("\n╭─ BESTCF · 优选链接自动同步 ─────────────────────────────╮")
    print(f"│ 时间：{now}（北京时间）")
    print(f"│ 来源：{args.source_url}")
    print(f"│ 输出：{args.output}")
    print("╰─────────────────────────────────────────────────────────╯")

    try:
        print("\n[1/4] 抓取优选页面")
        source = args.source_file.resolve().as_uri() if args.source_file else args.source_url
        source_html = fetch_html(source)
        domains, ips = extract_links(source_html)
        domain_count, ip_count = len(domains), len(ips)
        discovered = _unique(domains + ips)
        curated, selection_notes = select_highest_variants(discovered)
        discovered_subs = [item for item in curated if item.lower().startswith("sub://")]
        discovered_others = [item for item in curated if not item.lower().startswith("sub://")]
        subs = preserve_existing_order(old_subs, discovered_subs)
        others = preserve_existing_order(old_others, discovered_others)
        output_content = render_bestcf(subs, others)
        set_changed = set(old_subs) != set(subs) or set(old_others) != set(others)
        output_changed = old_raw != output_content or not args.output.exists()
        print(f"  ✓ 域名版块 {domain_count} 条；IP版块 {ip_count} 条")
        print(f"  ✓ 去重筛选后：sub {len(subs)} 条；普通链接 {len(others)} 条")
        for note in selection_notes:
            print(f"  · {note}")

        print("\n[2/4] 读取 yuan.txt 并计算分配")
        yuan_links = read_yuan_file(args.yuan)
        targets = build_panel_targets(subs, others, yuan_links)
        overflow_count = max(0, len(targets[2]) - len(yuan_links))
        print(f"  ✓ yuan.txt：{len(yuan_links)} 条")
        print(f"  ✓ 普通链接溢出：{overflow_count} 条（超过前两个普通链接面板的 98 条容量才产生）")

        if args.skip_panels:
            print("\n[3/4] 已按 --skip-panels 跳过远端面板")
            if output_changed:
                atomic_write(args.output, output_content)
                print("  ✓ bestcf.txt 已更新")
            else:
                print("  ✓ bestcf.txt 内容无变化，不重写")
            print("\n[4/4] 本地测试完成；未连接任何面板，也未更新日志")
            return 0

        print("\n[3/4] 读取四个面板并比较")
        password = resolve_admin_password()
        clients, snapshots = read_all_panels(targets, password)
        changed = [line_no for line_no, snap in snapshots.items() if snap.changed]
        print_panel_overview(snapshots, len(yuan_links), overflow_count)

        if args.dry_run:
            print("\n<预览模式> 只读取和比较，没有写入面板、bestcf.txt 或日志。")
            write_github_summary("预览完成（未写入）", now, len(subs), len(others), snapshots)
            return 0

        if not set_changed and not output_changed and not changed:
            print("\n╭─ ✓ 没有变化 ───────────────────────────────────────────╮")
            print("│ 链接集合、yuan.txt 分配及四个面板内容均一致。")
            print("│ 不改写 bestcf.txt，不改写日志，不写入任何面板。")
            print("╰───────────────────────────────────────────────────────╯")
            write_github_summary("无变化（未修改文件和面板）", now, len(subs), len(others), snapshots)
            return 0

        print("\n[4/4] 按顺序写入有变化的面板")
        completed: list[int] = []
        try:
            for line_no in (1, 4, 2, 3):
                snap = snapshots[line_no]
                if not snap.changed:
                    print(f"  = 第{line_no}行：内容一致，跳过")
                    continue
                print(f"  ↻ 第{line_no}行：写入 {len(snap.target_links)} 条…")
                clients[line_no].write_and_verify(snap.target_links)
                completed.append(line_no)
                print(f"  ✓ 第{line_no}行：写入成功，回读核验通过")
        except Exception as write_error:
            completed_text = ", ".join(map(str, completed)) or "无"
            raise BestCFError(
                f"面板写入失败：{write_error}。已完成并核验的行：{completed_text}。"
                "本版本不保存旧面板内容，也不自动回滚；再次运行会重新读取现状并按当前目标补齐。"
            ) from write_error

        if output_changed:
            atomic_write(args.output, output_content)
            print("  ✓ bestcf.txt 已更新")
        else:
            print("  = bestcf.txt 内容无变化，未重写")

        outcome = "成功：所有需要更新的面板均已写入，并通过回读核验。" if changed else "成功：面板原本已一致；仅本地链接文件发生变化。"
        report = make_report(now, domain_count, ip_count, subs, others, yuan_links,
                             targets, snapshots, outcome, selection_notes)
        atomic_write(args.log, report)
        print("\n╭─ ✓ BESTCF 运行完成 ────────────────────────────────────╮")
        print(f"│ sub 订阅：{len(subs)} 条；普通链接：{len(others)} 条")
        print(f"│ 第二行原始内容：{len(yuan_links)} 条；临时溢出：{overflow_count} 条")
        print(f"│ 本轮更新面板：{', '.join(map(str, changed)) if changed else '无'}")
        print(f"│ 日志：{args.log}")
        print("╰───────────────────────────────────────────────────────╯")
        write_github_summary("同步成功", now, len(subs), len(others), snapshots)
        return 0

    except Exception as exc:
        print("\n╭─ ✗ BESTCF 执行失败 ────────────────────────────────────╮", file=sys.stderr)
        print(f"│ 原因：{exc}", file=sys.stderr)
        print("│ 没有使用未验证的抓取结果覆盖本地链接文件。", file=sys.stderr)
        print("╰───────────────────────────────────────────────────────╯", file=sys.stderr)
        if domain_count or ip_count:
            partial_targets = targets or {1: [], 2: yuan_links, 3: subs, 4: []}
            report = make_report(now, domain_count, ip_count, subs, others, yuan_links,
                                 partial_targets, snapshots or None, f"失败：{exc}")
            try:
                atomic_write(args.log, report)
            except OSError:
                pass
        write_github_summary("执行失败", now, len(subs), len(others), snapshots or None)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
