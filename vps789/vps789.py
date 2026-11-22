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
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Literal

import httpx
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page, sync_playwright

PAGE_URL = "https://vps789.com/cfip/?remarks=domain"
TAIPEI_TZ = ZoneInfo("Asia/Taipei")
TOP20_API_URL = "https://vps789.com/openApi/cfIpTop20"
PORTS = (443, 8443, 2053, 2083, 2087, 2096)
NAVIGATION_TIMEOUT_MS = 60_000
TABLE_TIMEOUT_MS = 30_000
PAGINATION_TIMEOUT_MS = 30_000
# A changed page number is not proof that Element/Vue has repainted the table.
# Require the complete host sequence to remain stable before reading a page.
TABLE_STABLE_SECONDS = 1.0
TABLE_STABLE_POLL_SECONDS = 0.20
TABLE_STABLE_MIN_SAMPLES = 4
RETRY_COUNT = 3
PaginationStatus = Literal["more", "last", "unknown"]
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
    r"(?<![-+−\d.])(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>KB|MB|GB|TB)\s*/\s*s\b",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
LATENCY_RE = re.compile(r"(?<![-+−\d.])(\d+(?:\.\d+)?)\s*ms\b", re.IGNORECASE)
LOSS_RE = re.compile(r"(?<![-+−\d.])(\d+(?:\.\d+)?)\s*%")


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


def parse_metric_number(value: object, *, maximum: float | None = None) -> str:
    """将 API 的纯数字或带单位文本提取为有效的非负数字。"""
    if value is None or isinstance(value, bool):
        return ""
    match = NUMBER_RE.search(clean_text(str(value)))
    if not match:
        return ""
    try:
        number = float(match.group(0))
    except ValueError:
        return ""
    if number < 0 or (maximum is not None and number > maximum):
        return ""
    return format_number(number)


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
    """只选择当前可见的数据行，避免读到响应式布局留下的隐藏旧表格。"""
    for selector in DATA_ROW_SELECTORS:
        locator = page.locator(selector).filter(visible=True)
        if locator.count() > 0:
            return locator
    return page.locator("table tbody tr").filter(visible=True)


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
    rows = find_data_rows(page)
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
                candidate_loss = float(match.group(1))
                if 0 <= candidate_loss <= 100:
                    loss = format_number(candidate_loss)
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


def inspect_pagination_state(
    page: Page,
    total_count: int | None = None,
    page_size: int | None = None,
) -> tuple[bool, PaginationStatus, str]:
    """返回 (是否存在分页控件, MORE/LAST/UNKNOWN, 判断依据)。

    UNKNOWN 与 LAST 必须分开：没有找到下一页按钮，不等于已经确认到达末页。
    如果页面没有分页控件，则按单页表格处理；存在分页控件但证据不足时标记为未知。
    """
    script = r"""(known) => {
      const roots = Array.from(document.querySelectorAll('.el-pagination'))
        .filter(root => root.getClientRects().length > 0);
      if (!roots.length) {
        return { present: false, status: 'last', evidence: '未检测到可见分页控件，按单页表格处理' };
      }

      const toNumber = value => {
        const match = String(value || '').replace(/,/g, '').match(/\d+/);
        return match ? Number(match[0]) : null;
      };

      for (const root of roots) {
        const active = root.querySelector(
          '.el-pager li.number.active, .el-pager li.is-active, .el-pager .active, '
          + '.el-pager [aria-current="page"], .el-pager [aria-current="true"]'
        );
        const activeText = active
          ? (active.innerText || active.textContent || active.getAttribute('aria-label') || '')
          : '';
        const jumpInput = root.querySelector('.el-pagination__jump input');
        const currentPage = toNumber(activeText) || (jumpInput ? toNumber(jumpInput.value) : null);

        const pageItems = Array.from(root.querySelectorAll(
          '.el-pager li.number, .el-pager [data-page], .el-pager a, .el-pager button'
        ));
        const pageNumbers = pageItems
          .map(item => toNumber(item.getAttribute('data-page') || item.innerText || item.textContent))
          .filter(value => value !== null);
        const laterPage = currentPage === null
          ? undefined
          : pageNumbers.find(value => value > currentPage);
        if (laterPage !== undefined) {
          return {
            present: true,
            status: 'more',
            evidence: `页码列表显示当前第 ${currentPage} 页之后还有第 ${laterPage} 页`
          };
        }

        if (currentPage !== null && known.totalCount !== null && known.pageSize > 0) {
          const totalPages = Math.ceil(known.totalCount / known.pageSize);
          if (totalPages > currentPage) {
            return {
              present: true,
              status: 'more',
              evidence: `总记录数 ${known.totalCount}、每页 ${known.pageSize} 条，当前第 ${currentPage} 页，共 ${totalPages} 页`
            };
          }
          return {
            present: true,
            status: 'last',
            evidence: `总记录数 ${known.totalCount}、每页 ${known.pageSize} 条，当前第 ${currentPage} 页已到末页`
          };
        }

        const declaredPageCount = toNumber(
          root.getAttribute('data-page-count') || root.getAttribute('page-count')
        );
        if (declaredPageCount !== null && currentPage !== null) {
          return {
            present: true,
            status: declaredPageCount > currentPage ? 'more' : 'last',
            evidence: `分页控件声明共 ${declaredPageCount} 页，当前第 ${currentPage} 页`
          };
        }

        const next = root.querySelector(
          'button.btn-next, .btn-next, button[aria-label*="下一页"], button[aria-label*="Next"]'
        );
        if (next) {
          const classes = String(next.className || '').toLowerCase();
          const disabled = Boolean(next.disabled)
            || String(next.getAttribute('aria-disabled') || '').toLowerCase() === 'true'
            || classes.includes('is-disabled');
          return {
            present: true,
            status: disabled ? 'last' : 'more',
            evidence: disabled ? '分页控件的下一页按钮已禁用' : '分页控件的下一页按钮可用'
          };
        }
      }

      return {
        present: true,
        status: 'unknown',
        evidence: '存在分页控件，但无法确认是否已经到达末页'
      };
    }"""
    try:
        state = page.evaluate(
            script,
            {
                "totalCount": total_count,
                "pageSize": page_size or 0,
            },
        )
        if isinstance(state, dict):
            status = state.get("status")
            if status not in {"more", "last", "unknown"}:
                status = "unknown"
            return (
                bool(state.get("present")),
                status,  # type: ignore[return-value]
                clean_text(str(state.get("evidence") or "")),
            )
    except Exception as exc:
        try:
            present = page.locator(".el-pagination").filter(visible=True).count() > 0
        except Exception:
            present = True
        return (
            present,
            "unknown",
            f"分页状态检查失败：{type(exc).__name__}",
        )
    return False, "unknown", "未取得分页状态"


def row_data_signature(rows: Locator) -> tuple[str, ...]:
    """读取每行完整单元格文本，避免域名不在第一列时误判页面未变化。"""
    try:
        values = rows.evaluate_all(
            "elements => elements.map(row => { "
            "const cells = Array.from(row.querySelectorAll('td')).map(cell => "
            "(cell.innerText || cell.textContent || '').replace(/\\s+/g, ' ').trim()); "
            "return cells.length ? cells.join(' | ') : "
            "(row.innerText || row.textContent || '').replace(/\\s+/g, ' ').trim(); })"
        )
        return tuple(clean_text(str(value or "")) for value in values)
    except Exception:
        # 兼容少数无法执行 evaluate_all 的 Locator；慢速逐行读取只作回退。
        values: list[str] = []
        for index in range(rows.count()):
            values.append(clean_text(rows.nth(index).inner_text()))
        return tuple(values)


def table_loading_visible(page: Page) -> bool:
    """检查表格区域是否仍有可见的 Element loading 遮罩。"""
    script = r"""() => Array.from(document.querySelectorAll('.el-table .el-loading-mask')).some(el => {
      const style = window.getComputedStyle(el);
      const rect = el.getBoundingClientRect();
      return style.display !== 'none' && style.visibility !== 'hidden'
        && Number(style.opacity || '1') > 0.01 && rect.width > 0 && rect.height > 0;
    })"""
    try:
        return bool(page.evaluate(script))
    except Exception:
        # DOM 状态无法检查时宁可继续等待到超时，也不把未知状态当作已加载。
        return True


def parse_page_number(marker: str) -> int | None:
    match = re.fullmatch(r"\s*(\d+)\s*", clean_text(marker))
    return int(match.group(1)) if match else None


def pagination_total_count(page: Page) -> int | None:
    """读取 Element 分页组件的总记录数，例如“共 669 条”。"""
    locator = page.locator(".el-pagination__total").first
    try:
        if locator.count() == 0:
            return None
        match = re.search(r"(?<!\d)(\d[\d,]*)", clean_text(locator.inner_text()))
        return int(match.group(1).replace(",", "")) if match else None
    except PlaywrightError:
        return None


def expected_page_row_count(total_count: int | None, page_size: int, page_number: int | None) -> int | None:
    """依据总记录数、第一页实际页容量，计算指定页应有的行数。"""
    if total_count is None or total_count < 0 or page_size <= 0 or page_number is None or page_number < 1:
        return None
    start = (page_number - 1) * page_size
    if start >= total_count:
        return 0
    return min(page_size, total_count - start)


def wait_for_stable_table(
    page: Page,
    rows: Locator,
    *,
    previous_signature: tuple[str, ...] | None,
    expected_page: int | None,
    expected_row_count: int | None,
    timeout_ms: int,
) -> tuple[str, ...] | None:
    """等待整页行数据刷新并稳定；仅页码变化不足以确认表格已更新。"""
    deadline = time.monotonic() + timeout_ms / 1000
    stable_signature: tuple[str, ...] | None = None
    stable_since = 0.0
    stable_samples = 0

    while time.monotonic() < deadline:
        try:
            signature = row_data_signature(rows)
            row_count = rows.count()
            current_page = parse_page_number(active_page_marker(page))
            loading = table_loading_visible(page)
        except PlaywrightError:
            signature = ()
            row_count = 0
            current_page = None
            loading = True

        now = time.monotonic()
        page_matches = expected_page is None or current_page == expected_page
        count_matches = expected_row_count is None or row_count == expected_row_count
        signature_count_matches = len(signature) == row_count
        content_changed = previous_signature is None or signature != previous_signature

        ready = (
            bool(signature)
            and page_matches
            and count_matches
            and signature_count_matches
            and content_changed
            and not loading
        )
        if not ready:
            stable_signature = None
            stable_since = 0.0
            stable_samples = 0
        elif signature != stable_signature:
            stable_signature = signature
            stable_since = now
            stable_samples = 1
        else:
            stable_samples += 1
            if (
                stable_samples >= TABLE_STABLE_MIN_SAMPLES
                and now - stable_since >= TABLE_STABLE_SECONDS
            ):
                return signature

        time.sleep(TABLE_STABLE_POLL_SECONDS)

    return None


def wait_for_page_change(
    page: Page,
    rows: Locator,
    old_signature: tuple[str, ...],
    *,
    expected_page: int | None = None,
    expected_row_count: int | None = None,
) -> bool:
    """等待目标页码对应的整页数据刷新并稳定；超时保留已抓页面。"""
    result = wait_for_stable_table(
        page,
        rows,
        previous_signature=old_signature,
        expected_page=expected_page,
        expected_row_count=expected_row_count,
        timeout_ms=PAGINATION_TIMEOUT_MS,
    )
    return result is not None


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


def find_next_page_button(page: Page) -> Locator:
    """优先选择可见分页区的下一页控件，避免误选隐藏或无关按钮。"""
    for selector in (
        ".el-pagination button.btn-next",
        ".el-pagination .btn-next",
        "button.btn-next",
    ):
        locator = page.locator(selector).filter(visible=True)
        if locator.count() > 0:
            return locator.first
    return page.locator(".el-pagination .btn-next").filter(visible=True).first


def add_page_note(
    page_reports: list[dict[str, object]],
    key: str,
    note: str,
) -> None:
    if not page_reports:
        return
    existing = str(page_reports[-1].get(key) or "")
    page_reports[-1][key] = f"{existing}；{note}" if existing else note


def collect_page_domains(page: Page, diagnostics_dir: Path,
                         page_reports: list[dict[str, object]]) -> dict[str, DomainRecord]:
    wait_for_first_data_row(page)
    initial_rows = find_data_rows(page)
    initial_page = parse_page_number(active_page_marker(page))
    initial_signature = wait_for_stable_table(
        page,
        initial_rows,
        previous_signature=None,
        expected_page=initial_page,
        expected_row_count=None,
        timeout_ms=TABLE_TIMEOUT_MS,
    )
    if initial_signature is None:
        raise RuntimeError("第一页表格内容未能稳定；未读取不完整的初始数据。")

    # Page size is taken from the stabilized first page, then used with the
    # site's total count to validate short final pages (e.g. 669 -> 200/200/200/69).
    page_size = len(initial_signature)
    total_count = pagination_total_count(page)
    if total_count is not None and page_size:
        total_pages = (total_count + page_size - 1) // page_size
        last_page_rows = expected_page_row_count(total_count, page_size, total_pages)
        print(
            f"[网页] 分页信息：共 {total_count} 条；每页容量 {page_size} 行；"
            f"预计 {total_pages} 页；末页预计 {last_page_rows} 行。"
        )

    # Deduplicate while scraping so repeated pages do not remain resident in RAM.
    all_records: dict[str, DomainRecord] = {}
    cross_page_duplicates = 0
    page_no = 0

    while True:
        page_started = time.monotonic()
        page_no += 1
        rows = find_data_rows(page)
        row_count = rows.count()
        if row_count == 0:
            if page_no == 1:
                raise RuntimeError("第一页没有找到表格行。")
            note = "翻页后表格没有有效行；保留此前已抓取页面并停止后续翻页。"
            print(f"[网页] {note}")
            add_page_note(page_reports, "pagination_note", note)
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

        unrecognized_rows = max(0, row_count - valid_rows - row_read_failures)
        data_notes: list[str] = []
        if row_read_failures:
            data_notes.append(f"DOM 读取失败 {row_read_failures} 行")
        if unrecognized_rows:
            data_notes.append(f"未识别有效域名 {unrecognized_rows} 行")

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
            "elapsed_seconds": time.monotonic() - page_started,
            "skip_examples": skipped_examples,
        }
        if data_notes:
            page_report["data_note"] = "；".join(data_notes)
        page_reports.append(page_report)
        print(
            f"[网页] 第 {page_no} 页：原始 {row_count} 行；识别域名 {valid_rows} 条；"
            f"本页唯一 {len(page_records)}；本页重复 {page_duplicates}；"
            f"与前页重复 {page_cross_duplicates}；无法识别域名 {page_report['skipped_rows']} 行；"
            f"缺延迟 {missing_latency}、缺丢包率 {missing_loss}、缺速度 {missing_speed}；"
            f"DOM 读取失败 {row_read_failures}；累计全局唯一域名 {len(all_records)}；"
            f"本页耗时 {page_report['elapsed_seconds']:.1f} 秒。"
        )
        if skipped_examples:
            print("[网页] 跳过行示例：" + " || ".join(skipped_examples))
        save_page_snapshot(page, diagnostics_dir, page_no)

        next_button = find_next_page_button(page)
        if next_button.count() == 0:
            _, pagination_status, evidence = inspect_pagination_state(
                page, total_count=total_count, page_size=page_size
            )
            if pagination_status == "more":
                note = (
                    "已确认页面仍有后续页，但未识别到可点击的下一页按钮；"
                    f"{evidence}。保留已抓取页面并停止后续翻页。"
                )
            elif pagination_status == "unknown":
                note = (
                    "无法确认分页是否已经到达末页；"
                    f"{evidence}。本次结果按部分完成标记。"
                )
            else:
                note = "未发现后续分页，已到最后一页。"
            if pagination_status != "last":
                print(f"[网页] {note}")
                add_page_note(page_reports, "pagination_note", note)
                save_page_snapshot(page, diagnostics_dir, page_no, suffix="-pagination-unconfirmed")
            break
        try:
            class_text = (next_button.get_attribute("class") or "").casefold()
            aria_disabled = (next_button.get_attribute("aria-disabled") or "").casefold()
            if next_button.is_disabled() or aria_disabled == "true" or "is-disabled" in class_text:
                _, pagination_status, evidence = inspect_pagination_state(
                    page, total_count=total_count, page_size=page_size
                )
                if pagination_status == "more":
                    note = f"下一页按钮已禁用，但分页信息仍显示有后续页；{evidence}。"
                    print(f"[网页] {note}")
                    add_page_note(page_reports, "pagination_note", note)
                    save_page_snapshot(page, diagnostics_dir, page_no, suffix="-disabled-next-conflict")
                break
        except PlaywrightError as exc:
            note = f"读取下一页按钮状态失败（{type(exc).__name__}）；保留已抓取页面并停止后续翻页。"
            print(f"[网页] {note}")
            add_page_note(page_reports, "pagination_note", note)
            save_page_snapshot(page, diagnostics_dir, page_no, suffix="-next-state-failed")
            break

        old_signature = row_data_signature(rows)
        old_page_marker = active_page_marker(page)
        old_page_number = parse_page_number(old_page_marker)
        target_page = old_page_number + 1 if old_page_number is not None else None
        expected_rows = expected_page_row_count(total_count, page_size, target_page)
        try:
            next_button.click()
        except PlaywrightError as exc:
            note = f"点击下一页失败（{type(exc).__name__}）；保留已抓取页面并停止后续翻页。"
            print(f"[网页] {note}")
            add_page_note(page_reports, "pagination_note", note)
            save_page_snapshot(page, diagnostics_dir, page_no, suffix="-next-click-failed")
            break
        if not wait_for_page_change(
            page,
            rows,
            old_signature,
            expected_page=target_page,
            expected_row_count=expected_rows,
        ):
            expectation = []
            if target_page is not None:
                expectation.append(f"目标页码 {target_page}")
            if expected_rows is not None:
                expectation.append(f"预期 {expected_rows} 行")
            expected_text = "、".join(expectation) if expectation else "目标分页数据"
            note = (
                f"点击下一页后，{expected_text}未能在等待期内完成刷新并保持稳定；"
                "保留已抓取页面并停止后续翻页。"
            )
            print(f"[网页] {note}")
            add_page_note(page_reports, "pagination_note", note)
            save_page_snapshot(page, diagnostics_dir, page_no, suffix="-next-timeout")
            break

    captured_rows = sum(int(item.get("raw_rows", 0)) for item in page_reports)
    if total_count is not None and captured_rows != total_count:
        note = (
            f"网页分页声明共 {total_count} 行，实际读取 {captured_rows} 行；"
            "本次结果按部分完成标记。"
        )
        print(f"[网页] {note}")
        add_page_note(page_reports, "pagination_note", note)

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
        attempt_started = time.monotonic()
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

            records_by_key: dict[str, DomainRecord] = {}
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
                loss = parse_metric_number(item.get("dxPkgLostRate"), maximum=100)
                record = DomainRecord(
                    host=host,
                    info=format_info(latency=latency, loss=loss, speed=""),
                    source="api",
                )
                existing = records_by_key.get(record.key)
                if existing is not None:
                    duplicate_items += 1
                    records_by_key[record.key] = DomainRecord(
                        host=existing.host,
                        info=merge_record_info(existing.info, record.info),
                        source="api",
                    )
                    continue
                records_by_key[record.key] = record

            records = list(records_by_key.values())
            print(
                f"[API] 原始 {len(good)} 条；有效唯一域名 {len(records)} 个；"
                f"接口内重复 {duplicate_items} 条；无效项 {invalid_items} 条；"
                f"耗时 {time.monotonic() - attempt_started:.1f} 秒。"
            )
            return records
        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            print(
                f"[API] 请求失败（尝试 {attempt}/{API_RETRY_COUNT}）：{exc}；"
                f"耗时 {time.monotonic() - attempt_started:.1f} 秒。"
            )
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
    print(f"运行时间：{datetime.now(TAIPEI_TZ).strftime('%Y-%m-%d %H:%M:%S 台北时间')}")
    print(f"目标页面：{display_host(args.url) or args.url}")
    print(f"输出端口：{', '.join(map(str, PORTS))}")
    print("抓取规则：网页分页 + Top20 API；以域名为唯一键。")
    print("保留规则：域名有效即可保留；测速字段只作附加信息。")
    print("输出规则：每个端口文件每个域名一条；all.txt 按六端口顺序直接拼接。")
    print("")

    web_started = time.monotonic()
    web_records, page_reports, retry_notes = scrape_vps789_page(args.url, diagnostics_dir)
    web_elapsed = time.monotonic() - web_started
    total_rows = sum(int(item.get("raw_rows", 0)) for item in page_reports)
    skipped_rows = sum(int(item.get("skipped_rows", 0)) for item in page_reports)
    unique_web = len(web_records)
    web_duplicates = sum(
        int(item.get("duplicate_rows", 0)) + int(item.get("cross_page_duplicates", 0))
        for item in page_reports
    )

    print("\n┌─ 1. 网页分页结果")
    print("│ 页码｜原始行｜识别域名｜本页唯一｜本页重复｜跨页重复｜跳过行｜耗时")
    for item in page_reports:
        print(
            f"│ {int(item['page']):>3}｜{int(item['raw_rows']):>6,}｜"
            f"{int(item['valid_domains']):>8,}｜{int(item.get('unique_domains', 0)):>8,}｜"
            f"{int(item.get('duplicate_rows', 0)):>8,}｜"
            f"{int(item.get('cross_page_duplicates', 0)):>8,}｜"
            f"{int(item['skipped_rows']):>6,}｜"
            f"{float(item.get('elapsed_seconds', 0.0)):>5.1f}s"
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
        if item.get("data_note"):
            print(f"│   数据状态：{item['data_note']}")
        if item.get("pagination_note"):
            print(f"│   翻页状态：{item['pagination_note']}")
        if item.get("skip_examples"):
            for example in item["skip_examples"]:
                print(f"│   跳过示例：{example}")

    if retry_notes:
        print("\n网页打开重试：")
        for note in retry_notes:
            print(f"  · {note}")

    partial_notes: list[str] = []
    for item in page_reports:
        page_label = f"第 {int(item['page'])} 页"
        if item.get("data_note"):
            partial_notes.append(f"{page_label}：{item['data_note']}")
        if item.get("pagination_note"):
            partial_notes.append(f"{page_label}：{item['pagination_note']}")
    print(
        f"网页小计：读取 {total_rows:,} 行｜识别域名 {sum(int(item.get('valid_domains', 0)) for item in page_reports):,} 条｜"
        f"网页唯一 {unique_web:,} 个｜重复 {web_duplicates:,} 条｜"
        f"跳过 {skipped_rows:,} 行｜网页耗时 {web_elapsed:.1f} 秒"
    )
    if partial_notes:
        print(f"网页状态：部分完成，已保留 {len(page_reports)} 页中成功读取的结果。")
        for note in partial_notes:
            print(f"  · {note}")
    else:
        print(f"网页状态：正常完成，共处理 {len(page_reports)} 页。")

    print("\n┌─ 2. Top20 API 补充")
    api_started = time.monotonic()
    api_records = fetch_top20_api()
    api_elapsed = time.monotonic() - api_started
    print(f"│ API 可用域名：{len(api_records):,} 个｜总耗时 {api_elapsed:.1f} 秒")

    print("┌─ 3. 网页与 API 合并")
    merged = merge_sources(web_records, api_records, web_duplicates=web_duplicates)
    api_only = sum(record.source == "api" for record in merged.values())
    print(f"│ 网页唯一域名：{unique_web:,} 个")
    print(f"│ API 新增域名：{api_only:,} 个")
    print(f"│ 合并后唯一域名：{len(merged):,} 个")

    print("┌─ 4. 写入六个端口文件")
    output_started = time.monotonic()
    write_outputs(merged.values(), output_dir)
    output_elapsed = time.monotonic() - output_started
    for port in PORTS:
        path = output_dir / f"{port}.txt"
        print(f"│ {path.name:<10} {len(merged):>8,} 个域名｜{path.stat().st_size:,} bytes")
    all_path = output_dir / "all.txt"
    print(f"│ {'all.txt':<10} {len(merged) * len(PORTS):>8,} 条记录｜{all_path.stat().st_size:,} bytes")
    elapsed = time.monotonic() - started
    print(f"分项耗时：网页 {web_elapsed:.1f}s｜API {api_elapsed:.1f}s｜文件写入 {output_elapsed:.1f}s")
    if partial_notes:
        print("注意：本次网页抓取存在部分未读取或未确认的数据；端口文件仍按已取得网页与 API 结果生成。")
    print("\n" + rule)
    completion_label = "✓ 抓取完成（部分结果已标记）" if partial_notes else "✓ 抓取完成"
    print(f"{completion_label}｜唯一域名 {len(merged):,} 个｜总耗时 {elapsed:.1f} 秒")
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
            f"开始时间：{datetime.now(TAIPEI_TZ).strftime('%Y-%m-%d %H:%M:%S 台北时间')}\n"
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
            print("日志文件：vps789.log（本次运行已覆盖）")
            log_stream.flush()
            os.fsync(log_stream.fileno())

    print(f"日志已覆盖写入：{log_file}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
