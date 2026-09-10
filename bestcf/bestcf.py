#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
bestcf.py
从 wanwushequ/cfyxip 的 index.html 中提取：
  1. 优选域名 BestDomain
  2. 优选IP BestIP

输出：与本脚本同目录的 bestcf.txt

特点：
- 只提取两个指定 section，避免误收其他页面内容。
- 读取可见 .url-text、实际 href、sub:// 下拉选项 value。
- HTML 注释中的旧链接不会被提取。
- 自动精确去重，并保持网页出现顺序。
- 输出内容没变化时，不重写文件，保持文件 mtime 不变。
- 抓取失败、版块缺失或结果异常时，不覆盖现有 bestcf.txt。
- 支持重试、超时、Cache-Control: no-cache。
- 支持 --source-file 本地测试；正式运行无需参数。
"""

from __future__ import annotations

import argparse
import html
import os
import re
import sys
import tempfile
import time
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

SOURCE_URL = (
    "https://raw.githubusercontent.com/wanwushequ/cfyxip/"
    "refs/heads/main/index.html"
)
TARGET_SECTIONS = {
    "yxym": "优选域名 BestDomain",
    "yxip": "优选IP BestIP",
}
OUTPUT_NAME = "bestcf.txt"
URL_PATTERN = re.compile(r"^(?:https?://|sub://)\S+$", re.IGNORECASE)
HTML_CLASS_PATTERN = re.compile(r"(?:^|\s)url-text(?:\s|$)")


class BestCFParser(HTMLParser):
    """提取两个指定 section 内的实际链接，自动忽略 HTML 注释。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.section_depth = 0
        self.target_active = False
        self.current_section_target = False
        self.capture_tag: str | None = None
        self.capture_parts: list[str] = []
        self.section_links: dict[str, list[str]] = {"yxym": [], "yxip": []}
        self.current_key: str | None = None
        self.found_sections: set[str] = set()
        self._heading_parts: list[str] = []
        self._in_h2 = False

    @staticmethod
    def _normalize_candidate(value: str) -> str | None:
        value = html.unescape(value).strip()
        if not value:
            return None
        value = " ".join(value.split())
        if URL_PATTERN.fullmatch(value):
            return value
        return None

    @staticmethod
    def _section_key_from_heading(heading: str) -> str | None:
        normalized = " ".join(heading.split()).strip().lower()
        if normalized == "优选域名 bestdomain":
            return "yxym"
        if normalized == "优选ip bestip":
            return "yxip"
        return None

    def _add_link(self, key: str, value: str) -> None:
        if key not in self.section_links:
            return
        candidate = self._normalize_candidate(value)
        if candidate is not None:
            self.section_links[key].append(candidate)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr = dict(attrs)

        if tag == "section":
            self.section_depth += 1
            if self.section_depth == 1:
                section_id = (attr.get("id") or "").strip()
                self.current_section_target = section_id in TARGET_SECTIONS
                if self.current_section_target:
                    self.current_key = section_id
                    self.target_active = True
                    self.found_sections.add(section_id)

        if self.section_depth == 1 and tag == "h2":
            self._in_h2 = True
            self._heading_parts = []

        if self.target_active:
            class_value = attr.get("class") or ""
            if HTML_CLASS_PATTERN.search(class_value):
                self.capture_tag = tag
                self.capture_parts = []

            if tag == "option":
                value = attr.get("value")
                if value and value.lower().startswith("sub://"):
                    self._add_link(self.current_key or "", value)

            if tag == "a":
                href = attr.get("href")
                if href:
                    self._add_link(self.current_key or "", href)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_data(self, data: str) -> None:
        if self._in_h2:
            self._heading_parts.append(data)
        if self.capture_tag is not None and self.target_active:
            self.capture_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()

        if self.capture_tag == tag:
            self._add_link(self.current_key or "", "".join(self.capture_parts))
            self.capture_tag = None
            self.capture_parts = []

        if tag == "h2" and self._in_h2:
            heading = "".join(self._heading_parts)
            key = self._section_key_from_heading(heading)
            if key and self.section_depth == 1 and not self.target_active:
                # 无 id 页面时的后备识别方式。
                self.current_key = key
                self.target_active = True
                self.current_section_target = True
                self.found_sections.add(key)
            self._in_h2 = False
            self._heading_parts = []

        if tag == "section" and self.section_depth > 0:
            self.section_depth -= 1
            if self.section_depth == 0:
                self.target_active = False
                self.current_section_target = False
                self.current_key = None
                self.capture_tag = None
                self.capture_parts = []

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


def build_fresh_url(source_url: str) -> str:
    """给远端源增加时间戳参数，尽量绕开中间缓存。"""
    parts = urlsplit(source_url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["_bestcf_ts"] = str(int(time.time()))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def fetch_html(source_url: str, retries: int = 4, timeout: int = 30) -> str:
    last_error: Exception | None = None

    if source_url.startswith("file://"):
        from urllib.request import url2pathname
        from urllib.parse import unquote
        path = Path(url2pathname(unquote(urlsplit(source_url).path)))
        return path.read_text(encoding="utf-8")

    request_url = build_fresh_url(source_url)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; BestCF-Link-Collector/1.0)",
        "Accept": "text/html,application/xhtml+xml",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }

    for attempt in range(1, retries + 1):
        try:
            request = Request(request_url, headers=headers, method="GET")
            with urlopen(request, timeout=timeout) as response:
                data = response.read()
                charset = response.headers.get_content_charset() or "utf-8"
                return data.decode(charset, errors="replace")
        except Exception as exc:
            last_error = exc
            if attempt < retries:
                wait = 2 ** (attempt - 1)
                print(
                    f"[WARN] 抓取失败，第 {attempt}/{retries} 次：{exc}; {wait}s 后重试",
                    file=sys.stderr,
                )
                time.sleep(wait)

    raise RuntimeError(f"抓取失败，已重试 {retries} 次：{last_error}")


def extract_links(source_html: str) -> tuple[list[str], list[str]]:
    parser = BestCFParser()
    parser.feed(source_html)
    parser.close()

    missing = set(TARGET_SECTIONS) - parser.found_sections
    if missing:
        missing_text = ", ".join(TARGET_SECTIONS[key] for key in sorted(missing))
        raise RuntimeError(f"目标版块缺失：{missing_text}")

    domain_links, ip_links = parser.unique_links()
    if not domain_links:
        raise RuntimeError("优选域名 BestDomain 未提取到有效链接")
    if not ip_links:
        raise RuntimeError("优选IP BestIP 未提取到有效链接")
    return domain_links, ip_links


def read_existing(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def atomic_write(path: Path, lines: Iterable[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
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


def sync_output(output_path: Path, new_lines: list[str], domain_count: int, ip_count: int) -> bool:
    old_lines = read_existing(output_path)

    if old_lines == new_lines:
        print(
            f"[UNCHANGED] {output_path} 没有变化，保持原文件不动。"
            f" 域名={domain_count}，IP={ip_count}，总计={len(new_lines)}"
        )
        return False

    old_set = set(old_lines)
    new_set = set(new_lines)
    added = [line for line in new_lines if line not in old_set]
    removed = [line for line in old_lines if line not in new_set]

    atomic_write(output_path, new_lines)

    print(
        f"[UPDATED] {output_path} 已更新。"
        f" 域名={domain_count}，IP={ip_count}，总计={len(new_lines)}；"
        f"新增={len(added)}，删除={len(removed)}"
    )

    if added:
        print("[ADDED]")
        for line in added:
            print(f"  + {line}")
    if removed:
        print("[REMOVED]")
        for line in removed:
            print(f"  - {line}")

    return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="提取 BestDomain + BestIP 链接并同步到 bestcf.txt")
    parser.add_argument(
        "--source-url",
        default=os.environ.get("BESTCF_SOURCE_URL", SOURCE_URL),
        help="网页源地址；默认使用项目指定的 raw GitHub index.html",
    )
    parser.add_argument(
        "--source-file",
        type=Path,
        help="本地 HTML 测试文件（用于测试，不参与正常 GitHub Actions 运行）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().with_name(OUTPUT_NAME),
        help="输出文件；默认与 bestcf.py 同目录的 bestcf.txt",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    try:
        source_url = args.source_file.resolve().as_uri() if args.source_file else args.source_url
        print(f"[INFO] Source: {source_url}")
        print(f"[INFO] Output: {args.output}")

        source_html = fetch_html(source_url)
        domain_links, ip_links = extract_links(source_html)
        new_lines = domain_links + ip_links
        sync_output(args.output, new_lines, len(domain_links), len(ip_links))
        print(f"[OK] 提取完成，共 {len(new_lines)} 个唯一链接。")
        return 0
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        print("[SAFE] 本次未覆盖现有输出文件。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
