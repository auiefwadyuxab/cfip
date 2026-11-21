#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VPS789 优选域名聚合器。

抓取 VPS789 域名页的全部分页，并用 Top20 API 补充网页未出现的域名。
网页与 API 按规范化域名统一去重，测速字段仅作附加信息，不影响域名保留。
输出 443/8443/2053/2083/2087/2096 六个端口文件，以及按端口顺序
拼接六个文件内容的 all.txt。每个端口文件中每个域名只出现一次。

输出格式：域名:端口#电信延迟ms/电信丢包率-下载速度
例如：example.com:443#68ms/0-13.5MB/s
"""

from __future__ import annotations

import argparse
import contextlib
import ipaddress
import os
import re
import shutil
import sys
import tempfile
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import httpx
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, sync_playwright

PAGE_URL = "https://vps789.com/cfip/?remarks=domain"
TOP20_API_URL = "https://vps789.com/openApi/cfIpTop20"
PORTS = (443, 8443, 2053, 2083, 2087, 2096)
NAVIGATION_TIMEOUT_MS = 60_000
TABLE_TIMEOUT_MS = 30_000
PAGINATION_TIMEOUT_MS = 30_000
RETRY_COUNT = 3
API_RETRY_COUNT = 3
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

DATA_ROW_SELECTORS = (
    ".el-table__body-wrapper tbody tr.el-table__row",
    ".el-table__body tbody tr.el-table__row",
    "table tbody tr.el-table__row",
    ".el-table__body-wrapper tbody tr",
    ".el-table__body tbody tr",
    "table tbody tr",
)
DOMAIN_LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
DOWNLOAD_SPEED_RE = re.compile(
    r"(?P<number>-?\d+(?:\.\d+)?)\s*(?P<unit>KB|MB|GB|TB)\s*/\s*s\b",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
LATENCY_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*ms\b", re.IGNORECASE)
LOSS_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")


@dataclass(frozen=True, slots=True)
class DomainRecord:
    host: str
    info: str = ""
    source: Literal["web", "api"] = "web"
    _key: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "_key", normalize_host(self.host))

    @property
    def key(self) -> str:
        # Domain normalization can be relatively expensive; compute it once.
        return self._key


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def display_host(value: str) -> str:
    value = clean_text(value)
    value = re.sub(r"(?i)^https?://", "", value)
    value = value.split("/", 1)[0]
    return value.rstrip(".")


def normalize_host(value: str) -> str:
    """返回用于域名比较的标准形式，不改变节点文件里的首次显示形式。"""
    host = display_host(value)
    if not host:
        return ""
    try:
        return ipaddress.ip_address(host).compressed.casefold()
    except ValueError:
        pass
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return ""
    return host.rstrip(".").casefold()


def is_plausible_host(value: str) -> bool:
    """只接受完整域名或 IP，避免把延迟、速度等普通单词/数字误认成主机。"""
    host = display_host(value)
    if not host or any(char.isspace() for char in host):
        return False
    try:
        address = ipaddress.ip_address(host)
        return address.version == 4
    except ValueError:
        pass
    normalized = normalize_host(host)
    if not normalized or len(normalized) > 253 or "." not in normalized:
        return False
    labels = normalized.split(".")
    return all(label and len(label) <= 63 and DOMAIN_LABEL_RE.fullmatch(label) for label in labels)


def format_number(value: object) -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return clean_text(str(value))
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def parse_metric_number(value: object) -> str:
    """将 API 的纯数字或带单位文本提取为非负数字。"""
    if value is None or isinstance(value, bool):
        return ""
    match = NUMBER_RE.search(clean_text(str(value)))
    if not match:
        return ""
    try:
        number = float(match.group(0))
    except ValueError:
        return ""
    return format_number(number) if number >= 0 else ""


def format_info(latency: str = "", loss: str = "", speed: str = "") -> str:
    if not any((latency, loss, speed)):
        return ""
    latency_part = f"{latency}ms" if latency else ""
    return f"{latency_part}/{loss}-{speed}"


class RowReadError(RuntimeError):
    """A single table row could not be read while the page was being repainted."""


def get_row_cells(row: Locator) -> list[str]:
    """读取表格单行；常规文本读取失败时回退，仍失败则只标记该行。"""
    td_locator = row.locator("td")
    try:
        cells = [clean_text(value) for value in td_locator.all_inner_texts()]
    except Exception:
        cells = []
    if cells and any(cells):
        return cells
    try:
        return [
            clean_text(td_locator.nth(i).text_content() or "")
            for i in range(td_locator.count())
        ]
    except Exception as exc:
        raise RowReadError(f"读取单元格失败：{type(exc).__name__}: {exc}") from exc


def find_data_rows(page: Page) -> Locator:
    for selector in DATA_ROW_SELECTORS:
        locator = page.locator(selector)
        if locator.count() > 0:
            return locator
    return page.locator("table tbody tr")


def row_signature(rows: Locator, limit: int | None = 3) -> tuple[str, ...]:
    """Sample first/last table rows in one browser call for cheap page-change checks."""
    count = rows.count()
    if count <= 0:
        return ()
    sample = count if limit is None else min(limit, count)
    indexes = list(range(sample))
    indexes.extend(range(max(sample, count - sample), count))
    indexes = list(dict.fromkeys(indexes))
    try:
        values = rows.evaluate_all(
            "(elements, indexes) => indexes.map(i => { const row = elements[i]; "
            "return row ? (row.innerText || row.textContent || '').trim() : ''; })",
            indexes,
        )
        return tuple(clean_text(value) for value in values)
    except Exception:
        return tuple(clean_text(rows.nth(i).inner_text()) for i in indexes)


def get_all_rows_cells(rows: Locator, row_count: int) -> list[list[str] | None]:
    """Read all visible table rows in one DOM round-trip; isolate row-level failures."""
    try:
        values = rows.evaluate_all(
            "elements => elements.map(row => { try { "
            "return Array.from(row.querySelectorAll('td')).map(cell => "
            "(cell.innerText || cell.textContent || '').replace(/\\s+/g, ' ').trim()); "
            "} catch (error) { return null; } })"
        )
        result: list[list[str] | None] = []
        for item in values:
            if item is None:
                result.append(None)
            else:
                result.append([clean_text(value) for value in item])
        if len(result) == row_count:
            return result
    except Exception:
        pass

    # Compatibility fallback for older Playwright builds or unusual locators.
    result = []
    for index in range(row_count):
        try:
            result.append(get_row_cells(rows.nth(index)))
        except RowReadError:
            result.append(None)
    return result

def wait_until(deadline: float, predicate, interval: float = 0.10) -> bool:
    while time.monotonic() < deadline:
        try:
            if predicate():
                return True
        except PlaywrightError:
            # Vue/ElementPlus 重绘期间元素可能短暂脱离 DOM。
            pass
        time.sleep(interval)
    return False


def wait_for_first_data_row(page: Page) -> Locator:
    rows = page.locator(",".join(DATA_ROW_SELECTORS))
    deadline = time.monotonic() + TABLE_TIMEOUT_MS / 1000

    def ready() -> bool:
        return rows.count() > 0 and any(row_signature(rows, limit=min(5, rows.count())))

    if not wait_until(deadline, ready):
        raise RuntimeError("等待 VPS789 数据表超时：页面没有出现有效数据行。")
    return rows


def extract_domain_from_cells(cells: list[str]) -> tuple[int, str] | None:
    """先看域名列；页面列顺序变化时，再在其他单元格中寻找完整域名/IP。"""
    if cells:
        first = display_host(cells[0])
        if is_plausible_host(first):
            return 0, first
    for index, cell in enumerate(cells[1:], 1):
        candidate = display_host(cell)
        if is_plausible_host(candidate):
            return index, candidate
    return None


def extract_telecom_metric(cells: list[str]) -> tuple[str, str, int]:
    """延迟和丢包率分别读取；只出现其中一项时也保留已识别的信息。"""
    latency = ""
    loss = ""
    metric_index = -1
    for index, cell in enumerate(cells):
        if not latency:
            match = LATENCY_RE.search(cell)
            if match:
                latency = format_number(match.group(1))
                metric_index = index
        if not loss:
            match = LOSS_RE.search(cell)
            if match:
                loss = format_number(match.group(1))
                if metric_index < 0:
                    metric_index = index
        if latency and loss:
            break
    return latency, loss, metric_index


def extract_download_speed(cells: list[str], telecom_index: int) -> str:
    # 页面主要结构中下载速度通常位于第 5 列；其余列作为回退。
    if len(cells) > 4:
        match = DOWNLOAD_SPEED_RE.search(cells[4])
        if match:
            return f"{format_number(match.group('number'))}{match.group('unit').upper()}/s"
    start = telecom_index + 1 if telecom_index >= 0 else 0
    for cell in cells[start:]:
        match = DOWNLOAD_SPEED_RE.search(cell)
        if match:
            return f"{format_number(match.group('number'))}{match.group('unit').upper()}/s"
    return ""


def extract_record_from_row(cells: list[str]) -> DomainRecord | None:
    """A usable domain is sufficient; telemetry fields are optional."""
    domain_result = extract_domain_from_cells(cells)
    if domain_result is None:
        return None
    _, host = domain_result
    latency, loss, telecom_index = extract_telecom_metric(cells)
    speed = extract_download_speed(cells, telecom_index)
    return DomainRecord(host=host, info=format_info(latency, loss, speed), source="web")


def active_page_marker(page: Page) -> str:
    """读取常见 Element/ElementPlus 页码组件的当前页标记。"""
    selectors = (
        ".el-pager li.number.active",
        ".el-pager li.is-active",
        ".el-pager .number.active",
        ".el-pagination [aria-current='page']",
        "[aria-current='page']",
    )
    for selector in selectors:
        locator = page.locator(selector).first
        try:
            if locator.count():
                text = clean_text(locator.inner_text())
                if text:
                    return text
                aria = locator.get_attribute("aria-label") or ""
                if aria:
                    return aria
                classes = locator.get_attribute("class") or ""
                return f"{selector}:{classes}"
        except PlaywrightError:
            continue
    return ""


def wait_for_page_change(
    page: Page, rows: Locator, old_signature: str, old_page_marker: str, old_url: str = ""
) -> bool:
    """等待表格、活动页码或 URL 变化；超时返回 False，保留已抓页面。"""
    deadline = time.monotonic() + PAGINATION_TIMEOUT_MS / 1000

    def changed() -> bool:
        current_signature = "\x1f".join(row_signature(rows))
        current_marker = active_page_marker(page)
        current_url = str(getattr(page, "url", "") or "")
        rows_changed = bool(current_signature) and current_signature != old_signature
        page_changed = bool(old_page_marker and current_marker and current_marker != old_page_marker)
        url_changed = bool(old_url and current_url and current_url != old_url)
        return rows_changed or page_changed or url_changed

    return wait_until(deadline, changed)


def save_page_snapshot(page: Page, diagnostics_dir: Path, page_no: int, suffix: str = "") -> None:
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    stem = f"page-{page_no:02d}{suffix}"
    try:
        page.screenshot(path=str(diagnostics_dir / f"{stem}.png"), full_page=True)
    except Exception as exc:
        print(f"[诊断] 第 {page_no} 页截图保存失败：{type(exc).__name__}: {exc}")
    try:
        (diagnostics_dir / f"{stem}.html").write_text(page.content(), encoding="utf-8")
    except Exception as exc:
        print(f"[诊断] 第 {page_no} 页 HTML 保存失败：{type(exc).__name__}: {exc}")


def collect_page_domains(page: Page, diagnostics_dir: Path,
                         page_reports: list[dict[str, object]]) -> dict[str, DomainRecord]:
    wait_for_first_data_row(page)
    # Deduplicate while scraping so repeated pages do not remain resident in RAM.
    all_records: dict[str, DomainRecord] = {}
    cross_page_duplicates = 0
    page_no = 0

    while True:
        page_no += 1
        rows = find_data_rows(page)
        row_count = rows.count()
        if row_count == 0:
            if page_no == 1:
                raise RuntimeError("第一页没有找到表格行。")
            note = "翻页后表格没有有效行；保留此前已抓取页面并停止后续翻页。"
            print(f"[网页] {note}")
            if page_reports:
                page_reports[-1]["pagination_note"] = note
            save_page_snapshot(page, diagnostics_dir, page_no, suffix="-empty-after-next")
            break

        page_records: dict[str, DomainRecord] = {}
        skipped_examples: list[str] = []
        row_read_failures = 0
        valid_rows = 0
        missing_latency = 0
        missing_loss = 0
        missing_speed = 0
        row_cells = get_all_rows_cells(rows, row_count)

        for index, cells in enumerate(row_cells):
            if cells is None:
                row_read_failures += 1
                if len(skipped_examples) < 5:
                    skipped_examples.append(f"[第 {index + 1} 行读取失败] 元素在批量读取时已脱离 DOM")
                continue

            record = extract_record_from_row(cells)
            if record is None:
                if len(skipped_examples) < 5:
                    skipped_examples.append(" | ".join(cells[:5]))
                continue

            valid_rows += 1
            latency, loss, speed = split_info(record.info)
            missing_latency += int(not latency)
            missing_loss += int(not loss)
            missing_speed += int(not speed)
            existing_in_page = page_records.get(record.key)
            if existing_in_page is None:
                page_records[record.key] = record
            else:
                page_records[record.key] = DomainRecord(
                    host=existing_in_page.host,
                    info=merge_record_info(existing_in_page.info, record.info),
                    source="web",
                )

        page_duplicates = valid_rows - len(page_records)
        page_cross_duplicates = 0
        for key, record in page_records.items():
            existing = all_records.get(key)
            if existing is None:
                all_records[key] = record
            else:
                page_cross_duplicates += 1
                all_records[key] = DomainRecord(
                    host=existing.host,
                    info=merge_record_info(existing.info, record.info),
                    source="web",
                )
        cross_page_duplicates += page_cross_duplicates

        page_report = {
            "page": page_no,
            "raw_rows": row_count,
            "valid_domains": valid_rows,
            "unique_domains": len(page_records),
            "duplicate_rows": page_duplicates,
            "cross_page_duplicates": page_cross_duplicates,
            "skipped_rows": row_count - valid_rows,
            "missing_latency": missing_latency,
            "missing_loss": missing_loss,
            "missing_speed": missing_speed,
            "row_read_failures": row_read_failures,
            "skip_examples": skipped_examples,
        }
        page_reports.append(page_report)
        print(
            f"[网页] 第 {page_no} 页：原始 {row_count} 行；识别域名 {valid_rows} 条；"
            f"本页唯一 {len(page_records)}；本页重复 {page_duplicates}；"
            f"与前页重复 {page_cross_duplicates}；无法识别域名 {page_report['skipped_rows']} 行；"
            f"缺延迟 {missing_latency}、缺丢包率 {missing_loss}、缺速度 {missing_speed}；"
            f"DOM 读取失败 {row_read_failures}；累计全局唯一域名 {len(all_records)}。"
        )
        if skipped_examples:
            print("[网页] 跳过行示例：" + " || ".join(skipped_examples))
        save_page_snapshot(page, diagnostics_dir, page_no)

        next_button = page.locator("button.btn-next").first
        if next_button.count() == 0:
            next_button = page.locator(
                ".el-pagination button.btn-next, .el-pagination .btn-next"
            ).first
        if next_button.count() == 0:
            break
        try:
            class_text = (next_button.get_attribute("class") or "").casefold()
            aria_disabled = (next_button.get_attribute("aria-disabled") or "").casefold()
            if next_button.is_disabled() or aria_disabled == "true" or "is-disabled" in class_text:
                break
        except PlaywrightError as exc:
            note = f"读取下一页按钮状态失败（{type(exc).__name__}）；保留已抓取页面并停止后续翻页。"
            print(f"[网页] {note}")
            if page_reports:
                page_reports[-1]["pagination_note"] = note
            save_page_snapshot(page, diagnostics_dir, page_no, suffix="-next-state-failed")
            break

        old_signature = "\x1f".join(row_signature(rows))
        old_page_marker = active_page_marker(page)
        old_url = str(getattr(page, "url", "") or "")
        try:
            next_button.click()
        except PlaywrightError as exc:
            note = f"点击下一页失败（{type(exc).__name__}）；保留已抓取页面并停止后续翻页。"
            print(f"[网页] {note}")
            if page_reports:
                page_reports[-1]["pagination_note"] = note
            save_page_snapshot(page, diagnostics_dir, page_no, suffix="-next-click-failed")
            break
        if not wait_for_page_change(page, rows, old_signature, old_page_marker, old_url):
            note = "点击下一页后，表格、页码和 URL 在等待期内均未变化；保留已抓取页面并停止后续翻页。"
            print(f"[网页] {note}")
            if page_reports:
                page_reports[-1]["pagination_note"] = note
            save_page_snapshot(page, diagnostics_dir, page_no, suffix="-next-timeout")
            break

    print(f"[网页] 所有已取得页面合并后唯一域名：{len(all_records)}；跨页重复：{cross_page_duplicates}。")
    # Keep the insertion-ordered dictionary through the API merge and writer;
    # converting to a list here would duplicate the full record reference array.
    return all_records

def capture_page_debug(page: Page | None, diagnostics_dir: Path, attempt: int, error: Exception) -> None:
    diagnostics_dir.mkdir(parents=True, exist_ok=True)
    stem = f"failure-attempt-{attempt}"
    if page is not None:
        try:
            page.screenshot(path=str(diagnostics_dir / f"{stem}.png"), full_page=True)
        except Exception as exc:
            print(f"[诊断] 失败页面截图保存失败：{type(exc).__name__}: {exc}")
        try:
            (diagnostics_dir / f"{stem}.html").write_text(page.content(), encoding="utf-8")
        except Exception as exc:
            print(f"[诊断] 失败页面 HTML 保存失败：{type(exc).__name__}: {exc}")
    (diagnostics_dir / f"{stem}.txt").write_text(
        f"{type(error).__name__}: {error}\n", encoding="utf-8"
    )


def new_page(browser):
    context = browser.new_context(
        user_agent=USER_AGENT,
        ignore_https_errors=False,
        viewport={"width": 1440, "height": 1000},
        locale="zh-CN",
    )

    def route_handler(route):
        if route.request.resource_type in {"image", "media", "font"}:
            route.abort()
        else:
            route.continue_()

    context.route("**/*", route_handler)
    page = context.new_page()
    page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
    page.set_default_timeout(TABLE_TIMEOUT_MS)
    return context, page


def scrape_vps789_page(page_url: str, diagnostics_dir: Path) -> tuple[dict[str, DomainRecord], list[dict[str, object]], list[str]]:
    last_error: Exception | None = None
    retry_notes: list[str] = []
    for attempt in range(1, RETRY_COUNT + 1):
        browser = None
        context = None
        page = None
        attempt_reports: list[dict[str, object]] = []
        try:
            # Recreate the browser for each attempt. A crashed browser process
            # must not poison the following retries.
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    print(f"[网页] 打开 VPS789（尝试 {attempt}/{RETRY_COUNT}）")
                    context, page = new_page(browser)
                    page.goto(page_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
                    records = collect_page_domains(page, diagnostics_dir, attempt_reports)
                    if not records:
                        raise RuntimeError("所有网页分页都没有识别到有效域名。")
                    return records, attempt_reports, retry_notes
                finally:
                    if context is not None:
                        context.close()
                    if browser is not None:
                        browser.close()
        except Exception as exc:
            last_error = exc
            note = f"第 {attempt}/{RETRY_COUNT} 次网页抓取失败：{type(exc).__name__}: {exc}"
            retry_notes.append(note)
            print(f"[网页] {note}")
            capture_page_debug(page, diagnostics_dir, attempt, exc)
            if attempt < RETRY_COUNT:
                time.sleep(min(2 ** (attempt - 1), 4))
    raise RuntimeError(f"VPS789 网页抓取失败：{last_error}") from last_error


def fetch_top20_api() -> list[DomainRecord]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Cache-Control": "no-cache",
    }
    last_error: Exception | None = None
    for attempt in range(1, API_RETRY_COUNT + 1):
        try:
            with httpx.Client(
                timeout=httpx.Timeout(20.0, connect=10.0),
                follow_redirects=True,
                headers=headers,
            ) as client:
                response = client.get(TOP20_API_URL)
                response.raise_for_status()
                payload = response.json()

            if not isinstance(payload, dict):
                raise ValueError("返回内容不是 JSON 对象")
            code = payload.get("code")
            if code not in (0, "0", None):
                raise ValueError(f"接口 code={code}")
            data = payload.get("data")
            if not isinstance(data, dict):
                raise ValueError("返回内容缺少 data 对象")
            good = data.get("good")
            if not isinstance(good, list):
                raise ValueError("data.good 不是数组")

            records: list[DomainRecord] = []
            seen: set[str] = set()
            invalid_items = 0
            duplicate_items = 0
            for item in good:
                if not isinstance(item, dict):
                    invalid_items += 1
                    continue
                host = display_host(str(item.get("ip") or ""))
                if not is_plausible_host(host):
                    invalid_items += 1
                    continue
                latency = parse_metric_number(item.get("dxLatency"))
                loss = parse_metric_number(item.get("dxPkgLostRate"))
                record = DomainRecord(
                    host=host,
                    info=format_info(latency=latency, loss=loss, speed=""),
                    source="api",
                )
                if record.key in seen:
                    duplicate_items += 1
                    continue
                seen.add(record.key)
                records.append(record)

            print(
                f"[API] 原始 {len(good)} 条；有效域名 {len(records)} 个；"
                f"接口内重复 {duplicate_items} 条；无效项 {invalid_items} 条。"
            )
            return records
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            print(f"[API] 请求失败（尝试 {attempt}/{API_RETRY_COUNT}）：{exc}")
            if attempt < API_RETRY_COUNT:
                time.sleep(min(2 ** (attempt - 1), 4))
    print(f"[API] {API_RETRY_COUNT} 次请求均失败，跳过 API 补充：{last_error}")
    return []


def split_info(info: str) -> tuple[str, str, str]:
    if not info:
        return "", "", ""
    latency = ""
    loss = ""
    speed = ""
    latency_match = re.search(r"(-?\d+(?:\.\d+)?)ms", info, re.IGNORECASE)
    if latency_match:
        latency = latency_match.group(1)
    if "/" in info:
        tail = info.split("/", 1)[1]
        loss_part, separator, speed_part = tail.partition("-")
        loss = clean_text(loss_part)
        speed = clean_text(speed_part) if separator else ""
    return latency, loss, speed


def merge_record_info(first: str, later: str) -> str:
    first_parts = split_info(first)
    later_parts = split_info(later)
    merged = tuple(a or b for a, b in zip(first_parts, later_parts))
    return format_info(*merged)


def merge_sources(
    web_records: dict[str, DomainRecord] | list[DomainRecord],
    api_records: list[DomainRecord],
    web_duplicates: int = 0,
) -> dict[str, DomainRecord]:
    """Merge by normalized domain while retaining first-seen web order.

    Normal scraper flow passes its insertion-ordered dictionary directly, so
    the merge updates it in place instead of building a second list and index.
    The list form remains accepted for small helper callers and tests.
    """
    if isinstance(web_records, dict):
        merged = web_records
        web_count = len(merged)
    else:
        merged: dict[str, DomainRecord] = {}
        web_count = len(web_records)
        for record in web_records:
            existing = merged.get(record.key)
            if existing is None:
                merged[record.key] = record
            else:
                web_duplicates += 1
                merged[record.key] = DomainRecord(
                    host=existing.host,
                    info=merge_record_info(existing.info, record.info),
                    source="web",
                )

    api_duplicates = 0
    api_added = 0
    for record in api_records:
        existing = merged.get(record.key)
        if existing is not None:
            api_duplicates += 1
            merged[record.key] = DomainRecord(
                host=existing.host,
                info=merge_record_info(existing.info, record.info),
                source=existing.source,
            )
            continue
        merged[record.key] = record
        api_added += 1

    print(
        f"[合并] 网页唯一域名 {web_count} 个；网页分页重复域名 {web_duplicates} 条；"
        f"API 记录 {len(api_records)} 条；其中重复/与网页重合 {api_duplicates} 条；"
        f"API 新增域名 {api_added} 个；最终唯一域名 {len(merged)} 个。"
    )
    return merged


def _sha256_file(path: Path) -> str:
    import hashlib
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_outputs(records, output_dir: Path) -> None:
    """Stream each port file and all.txt without materializing another record set."""
    output_dir.mkdir(parents=True, exist_ok=True)

    output_names = [f"{port}.txt" for port in PORTS] + ["all.txt"]
    destinations = [output_dir / name for name in output_names]
    replaced: list[Path] = []

    with tempfile.TemporaryDirectory(prefix=".vps789-stage-", dir=output_dir) as stage_name:
        stage_dir = Path(stage_name)
        backups: dict[Path, Path | None] = {}
        for destination in destinations:
            if destination.exists():
                backup = stage_dir / f"backup-{destination.name}"
                shutil.copy2(destination, backup)
                backups[destination] = backup
            else:
                backups[destination] = None

        staged_ports: list[Path] = []
        for port in PORTS:
            stage_path = stage_dir / f"{port}.txt"
            with stage_path.open("w", encoding="utf-8", newline="\n", buffering=1024 * 1024) as handle:
                for record in records:
                    handle.write(f"{record.host}:{port}#{record.info}\n")
                handle.flush()
                os.fsync(handle.fileno())
            staged_ports.append(stage_path)

        staged_all = stage_dir / "all.txt"
        with staged_all.open("wb", buffering=1024 * 1024) as output:
            for staged_port in staged_ports:
                with staged_port.open("rb") as source:
                    shutil.copyfileobj(source, output, length=1024 * 1024)
            output.flush()
            os.fsync(output.fileno())

        staged_files = staged_ports + [staged_all]
        expected = {
            destination: _sha256_file(staged)
            for destination, staged in zip(destinations, staged_files)
        }
        try:
            for destination, staged in zip(destinations, staged_files):
                staged.replace(destination)
                replaced.append(destination)
            for destination in destinations:
                if _sha256_file(destination) != expected[destination]:
                    raise RuntimeError(f"{destination.name} 写入后 SHA-256 回读不一致")
                print(f"[输出] {destination.name}：{destination.stat().st_size:,} bytes")
        except Exception as write_error:
            rollback_errors: list[str] = []
            for destination in reversed(replaced):
                backup = backups[destination]
                try:
                    if backup is None:
                        destination.unlink(missing_ok=True)
                    else:
                        restore = stage_dir / f"restore-{destination.name}"
                        shutil.copy2(backup, restore)
                        restore.replace(destination)
                except OSError as rollback_error:
                    rollback_errors.append(f"{destination.name}: {rollback_error}")
            if rollback_errors:
                raise RuntimeError(
                    f"输出写入失败：{write_error}；回滚也有失败：{'；'.join(rollback_errors)}"
                ) from write_error
            raise


class Tee:
    """Write console output to both Actions stdout and the current-run log immediately."""

    def __init__(self, stream, log_stream) -> None:
        self.stream = stream
        self.log_stream = log_stream

    def write(self, value: str) -> int:
        written = self.stream.write(value)
        self.log_stream.write(value)
        # A timeout or forced process stop must not discard the in-memory log.
        self.log_stream.flush()
        return written

    def flush(self) -> None:
        self.stream.flush()
        self.log_stream.flush()


def reset_diagnostics(diagnostics_dir: Path) -> None:
    if diagnostics_dir.exists():
        shutil.rmtree(diagnostics_dir)
    diagnostics_dir.mkdir(parents=True, exist_ok=True)


def run_scrape(args: argparse.Namespace, output_dir: Path, diagnostics_dir: Path) -> int:
    rule = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    started = time.monotonic()
    print(rule)
    print("VPS789 · 域名抓取运行报告")
    print(rule)
    print(f"运行时间：{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f"目标页面：{display_host(args.url) or args.url}")
    print(f"输出端口：{', '.join(map(str, PORTS))}")
    print("抓取规则：网页分页 + Top20 API；以域名为唯一键。")
    print("保留规则：域名有效即可保留；测速字段只作附加信息。")
    print("输出规则：每个端口文件每个域名一条；all.txt 按六端口顺序直接拼接。")
    print("")

    web_records, page_reports, retry_notes = scrape_vps789_page(args.url, diagnostics_dir)
    total_rows = sum(int(item.get("raw_rows", 0)) for item in page_reports)
    skipped_rows = sum(int(item.get("skipped_rows", 0)) for item in page_reports)
    unique_web = len(web_records)
    web_duplicates = sum(
        int(item.get("duplicate_rows", 0)) + int(item.get("cross_page_duplicates", 0))
        for item in page_reports
    )

    print("\n┌─ 1. 网页分页结果")
    print("│ 页码｜原始行｜识别域名｜本页唯一｜本页重复｜跨页重复｜跳过行")
    for item in page_reports:
        print(
            f"│ {int(item['page']):>3}｜{int(item['raw_rows']):>6,}｜"
            f"{int(item['valid_domains']):>8,}｜{int(item.get('unique_domains', 0)):>8,}｜"
            f"{int(item.get('duplicate_rows', 0)):>8,}｜"
            f"{int(item.get('cross_page_duplicates', 0)):>8,}｜"
            f"{int(item['skipped_rows']):>6,}"
        )
        missing_latency = int(item.get("missing_latency", 0))
        missing_loss = int(item.get("missing_loss", 0))
        missing_speed = int(item.get("missing_speed", 0))
        if missing_latency or missing_loss or missing_speed:
            print(
                f"│   缺少测速信息：延迟 {missing_latency} 行｜"
                f"丢包率 {missing_loss} 行｜速度 {missing_speed} 行"
            )
        if item.get("row_read_failures"):
            print(f"│   页面元素读取失败：{item['row_read_failures']} 行")
        if item.get("pagination_note"):
            print(f"│   翻页状态：{item['pagination_note']}")
        if item.get("skip_examples"):
            for example in item["skip_examples"]:
                print(f"│   跳过示例：{example}")

    if retry_notes:
        print("\n网页打开重试：")
        for note in retry_notes:
            print(f"  · {note}")

    pagination_notes = [
        str(item["pagination_note"])
        for item in page_reports
        if item.get("pagination_note")
    ]
    print(
        f"网页小计：读取 {total_rows:,} 行｜识别域名 {sum(int(item.get('valid_domains', 0)) for item in page_reports):,} 条｜"
        f"网页唯一 {unique_web:,} 个｜重复 {web_duplicates:,} 条｜"
        f"跳过 {skipped_rows:,} 行"
    )
    if pagination_notes:
        print(f"网页状态：部分分页，已保留 {len(page_reports)} 页有效结果。")
        print(f"停止原因：{pagination_notes[-1]}")
    else:
        print(f"网页状态：正常完成，共处理 {len(page_reports)} 页。")

    print("\n┌─ 2. Top20 API 补充")
    api_records = fetch_top20_api()
    print(f"│ API 可用域名：{len(api_records):,} 个")

    print("┌─ 3. 网页与 API 合并")
    merged = merge_sources(web_records, api_records, web_duplicates=web_duplicates)
    api_only = sum(record.source == "api" for record in merged.values())
    print(f"│ 网页唯一域名：{unique_web:,} 个")
    print(f"│ API 新增域名：{api_only:,} 个")
    print(f"│ 合并后唯一域名：{len(merged):,} 个")

    print("┌─ 4. 写入六个端口文件")
    write_outputs(merged.values(), output_dir)
    for port in PORTS:
        path = output_dir / f"{port}.txt"
        print(f"│ {path.name:<10} {len(merged):>8,} 个域名｜{path.stat().st_size:,} bytes")
    all_path = output_dir / "all.txt"
    print(f"│ {'all.txt':<10} {len(merged) * len(PORTS):>8,} 条记录｜{all_path.stat().st_size:,} bytes")
    elapsed = time.monotonic() - started
    print("\n" + rule)
    print(f"✓ 抓取完成｜唯一域名 {len(merged):,} 个｜总耗时 {elapsed:.1f} 秒")
    if pagination_notes:
        print("注意：本次网页分页未完整到达末页，端口文件按已抓取页面与 API 结果生成。")
    print(rule)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取 VPS789 全部网页分页，以 Top20 API 补充，并按域名去重生成六个端口文件和 all.txt。"
    )
    parser.add_argument("--url", default=PAGE_URL, help="VPS789 域名页面地址。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(__file__).resolve().parent
    diagnostics_dir = (
        Path(os.getenv("VPS789_DIAGNOSTICS_DIR", "")).expanduser()
        if os.getenv("VPS789_DIAGNOSTICS_DIR")
        else Path(tempfile.gettempdir()) / "vps789-diagnostics"
    )
    log_file = output_dir / "vps789.log"
    code = 1

    # Overwrite at the start, then flush every write. If Actions times out or
    # terminates Chromium/Python, the log still contains all completed steps.
    with log_file.open("w", encoding="utf-8", newline="\n", buffering=1) as log_stream:
        log_stream.write(
            "╔════════════════════════════════════════════════════════════════════╗\n"
            "║                    VPS789 · 本次运行日志                           ║\n"
            "╚════════════════════════════════════════════════════════════════════╝\n"
            f"开始时间：{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
            "本文件每次运行覆盖；仅记录当前这次抓取。\n\n"
        )
        stdout_tee = Tee(sys.stdout, log_stream)
        stderr_tee = Tee(sys.stderr, log_stream)
        started = time.monotonic()
        with contextlib.redirect_stdout(stdout_tee), contextlib.redirect_stderr(stderr_tee):
            try:
                reset_diagnostics(diagnostics_dir)
                code = run_scrape(args, output_dir, diagnostics_dir)
            except Exception as exc:
                print(f"\n✗ VPS789 运行失败：{type(exc).__name__}: {exc}", file=sys.stderr)
                traceback.print_exc(file=sys.stderr)
                code = 1
            elapsed = time.monotonic() - started
            if code == 0:
                print(f"\n运行状态：成功｜总耗时 {elapsed:.1f} 秒")
            else:
                print(f"\n运行状态：失败｜总耗时 {elapsed:.1f} 秒")
            print(f"日志文件：{log_file}")
            log_stream.flush()
            os.fsync(log_stream.fileno())

    print(f"日志已覆盖写入：{log_file}")
    return code


