#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VPS789 优选域名聚合器

数据源：
1. VPS789 动态优选域名页：每约 30 分钟更新
   https://vps789.com/cfip/?remarks=domain
2. VPS789 官方 Top20 API：通常每日刷新
   https://vps789.com/openApi/cfIpTop20

输出：
    443.txt
    8443.txt
    2053.txt
    2083.txt
    2087.txt
    2096.txt

每行格式：
    域名:端口#延迟/丢包率-下载速度

例如：
    example.com:443#78/0-1234KB/s
    example.com:8443#91/1-
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import httpx
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


PAGE_URL = "https://vps789.com/cfip/?remarks=domain"
TOP20_API_URL = "https://vps789.com/openApi/cfIpTop20"

PORTS = (443, 8443, 2053, 2083, 2087, 2096)

# 默认取 200 条，明显超过用户要求的 100 条。
TARGET_COUNT = 200
MIN_COUNT = 100

NAVIGATION_TIMEOUT_MS = 60_000
TABLE_TIMEOUT_MS = 30_000
DOM_SETTLE_MS = 800
MAX_PAGES = 200

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

LATENCY_KEYS = ("延迟", "latency", "ping")
LOSS_KEYS = ("丢包", "丢包率", "loss", "packet loss")
SPEED_KEYS = ("下载", "下载速度", "速度", "download", "speed")
TELECOM_KEYS = ("电信", "telecom", "ctcc")

SPEED_UNIT_RE = re.compile(
    r"(?:KB|MB|GB|TB)\s*/?\s*s\b|"
    r"(?:Kbps|Mbps|Gbps|Tbps)\b",
    re.IGNORECASE,
)
NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")
LATENCY_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*ms\b", re.I)
LOSS_RE = re.compile(r"(-?\d+(?:\.\d+)?)\s*%")
HOST_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9._:-]{0,251}[A-Za-z0-9])?)$"
)


@dataclass(frozen=True)
class DomainRecord:
    host: str
    info: str = ""

    @property
    def key(self) -> str:
        return normalize_host(self.host)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_host(value: str) -> str:
    """
    用于“完全一样”的去重比较：
    - 去首尾空白
    - 去掉 URL scheme
    - 去掉末尾 /
    - 去掉末尾根域名点
    - 大小写归一化
    不主动改写子域名、IDN 或其它合法主机名内容。
    """
    value = clean_text(value)
    value = re.sub(r"(?i)^https?://", "", value)
    value = value.split("/", 1)[0]
    value = value.rstrip(".")
    return value.casefold()


def display_host(value: str) -> str:
    value = clean_text(value)
    value = re.sub(r"(?i)^https?://", "", value)
    value = value.split("/", 1)[0]
    return value.rstrip(".")


def is_plausible_host(value: str) -> bool:
    host = display_host(value)
    if not host or " " in host:
        return False
    return bool(HOST_RE.fullmatch(host))


def format_number(value: str | float | int | None) -> str:
    if value is None or value == "":
        return ""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return clean_text(str(value))
    if number.is_integer():
        return str(int(number))
    return f"{number:.2f}".rstrip("0").rstrip(".")


def parse_numeric(text: str, unit_kind: str) -> str:
    """
    从页面单元格提取数值：
    latency: 优先找 xxxms，否则取首个数字
    loss: 优先找 xx%，否则取首个数字
    speed: 优先找带单位的值，否则取首个数字
    """
    text = clean_text(text)
    if not text:
        return ""

    if unit_kind == "latency":
        match = LATENCY_RE.search(text)
        if match:
            return format_number(match.group(1))
    elif unit_kind == "loss":
        match = LOSS_RE.search(text)
        if match:
            return format_number(match.group(1))

    match = NUMBER_RE.search(text)
    return format_number(match.group(0)) if match else ""


def extract_speed(text: str, header: str = "") -> str:
    text = clean_text(text)
    if not text:
        return ""

    # 优先保留页面自身显示的单位。
    unit_match = SPEED_UNIT_RE.search(text)
    unit = clean_text(unit_match.group(0)) if unit_match else ""

    number_match = NUMBER_RE.search(text)
    if not number_match:
        return ""

    number = format_number(number_match.group(0))

    # 如果页面单元格只有数字，则从列头推断单位。
    if not unit:
        unit_match = SPEED_UNIT_RE.search(header)
        if unit_match:
            unit = clean_text(unit_match.group(0))

    return f"{number}{unit}" if unit else number


def format_info(latency: str = "", loss: str = "", speed: str = "") -> str:
    """
    严格保持：
        延迟/丢包率-下载速度
    速度没有时保留末尾 '-'，例如 78/0-
    """
    if not any((latency, loss, speed)):
        return ""
    return f"{latency}/{loss}-{speed}"


def header_is(header: str, keys: Iterable[str]) -> bool:
    h = clean_text(header).casefold()
    return any(key.casefold() in h for key in keys)


def build_header_paths(page: Page) -> list[str]:
    """
    ElementPlus 表格可能使用多行表头 + colspan/rowspan。
    这里把层级表头展开为：
        电信 / 延迟
        电信 / 丢包率
        电信 / 下载速度
    从而不依赖固定列号。
    """
    rows = page.locator("thead tr")
    row_count = rows.count()
    if row_count == 0:
        rows = page.locator(".el-table__header-wrapper tr")
        row_count = rows.count()

    if row_count == 0:
        return []

    grid: list[list[str | None]] = []

    for r in range(row_count):
        if len(grid) <= r:
            grid.append([])

        ths = rows.nth(r).locator("th")
        c = 0

        for i in range(ths.count()):
            th = ths.nth(i)

            while c < len(grid[r]) and grid[r][c] is not None:
                c += 1

            text = clean_text(th.inner_text())
            try:
                colspan = int(th.get_attribute("colspan") or "1")
            except ValueError:
                colspan = 1
            try:
                rowspan = int(th.get_attribute("rowspan") or "1")
            except ValueError:
                rowspan = 1

            for rr in range(r, r + rowspan):
                while len(grid) <= rr:
                    grid.append([])
                while len(grid[rr]) < c + colspan:
                    grid[rr].append(None)

                for cc in range(c, c + colspan):
                    old = grid[rr][cc]
                    if old and text and text not in old.split(" / "):
                        grid[rr][cc] = f"{old} / {text}"
                    elif old is None:
                        grid[rr][cc] = text

            c += colspan

    if not grid:
        return []

    width = max(len(row) for row in grid)
    paths: list[str] = []

    for col in range(width):
        parts: list[str] = []
        for row in grid:
            if col < len(row) and row[col]:
                part = clean_text(row[col] or "")
                if part and part not in parts:
                    parts.append(part)
        paths.append(" / ".join(parts))

    return paths


def find_data_rows(page: Page) -> Locator:
    selectors = (
        ".el-table__body-wrapper tbody tr.el-table__row",
        ".el-table__body tbody tr.el-table__row",
        "table tbody tr.el-table__row",
        ".el-table__row",
    )
    for selector in selectors:
        locator = page.locator(selector)
        if locator.count():
            return locator
    return page.locator("table tbody tr")


def find_domain_column(header_paths: list[str], cell_count: int) -> int:
    preferred_keys = ("域名", "hostname", "host", "ip")
    for i, header in enumerate(header_paths[:cell_count]):
        if header_is(header, preferred_keys):
            return i
    return 0


def find_metric_columns(
    header_paths: list[str], cell_count: int
) -> dict[str, list[int]]:
    result = {"latency": [], "loss": [], "speed": []}

    for i, header in enumerate(header_paths[:cell_count]):
        if not header:
            continue
        is_telecom = header_is(header, TELECOM_KEYS)
        if not is_telecom:
            continue

        if header_is(header, LATENCY_KEYS):
            result["latency"].append(i)
        elif header_is(header, LOSS_KEYS):
            result["loss"].append(i)
        elif header_is(header, SPEED_KEYS):
            result["speed"].append(i)

    return result


def extract_record_from_row(
    cells: list[str], header_paths: list[str]
) -> DomainRecord | None:
    if not cells:
        return None

    domain_col = find_domain_column(header_paths, len(cells))
    host = display_host(cells[domain_col])
    if not is_plausible_host(host):
        # 某些表格可能存在序号/空白固定列，回退到第一列看起来像域名的值。
        for cell in cells:
            candidate = display_host(cell)
            if is_plausible_host(candidate) and "." in candidate:
                host = candidate
                break
        else:
            return None

    columns = find_metric_columns(header_paths, len(cells))

    latency = ""
    loss = ""
    speed = ""

    for idx in columns["latency"]:
        latency = parse_numeric(cells[idx], "latency")
        if latency:
            break

    for idx in columns["loss"]:
        loss = parse_numeric(cells[idx], "loss")
        if loss:
            break

    for idx in columns["speed"]:
        header = header_paths[idx] if idx < len(header_paths) else ""
        speed = extract_speed(cells[idx], header)
        if speed:
            break

    return DomainRecord(host=host, info=format_info(latency, loss, speed))


def collect_page_domains(
    page: Page,
    page_limit: int = MAX_PAGES,
) -> list[DomainRecord]:
    page.wait_for_selector(
        ".el-table__row, table tbody tr, .el-table__body-wrapper tbody tr",
        timeout=TABLE_TIMEOUT_MS,
    )
    time.sleep(DOM_SETTLE_MS / 1000)

    all_records: list[DomainRecord] = []
    seen_page_signatures: set[str] = set()
    header_paths = build_header_paths(page)

    for page_no in range(1, page_limit + 1):
        rows = find_data_rows(page)
        row_count = rows.count()

        if row_count == 0:
            raise RuntimeError(f"第 {page_no} 页没有找到数据行。")

        # 某些 ElementPlus 页面会在翻页期间短暂残留旧 DOM。
        # 先读取一遍当前页，构造签名，避免同一页被重复追加。
        page_records: list[DomainRecord] = []
        for i in range(row_count):
            row = rows.nth(i)
            cells = [clean_text(x) for x in row.locator("td").all_inner_texts()]
            record = extract_record_from_row(cells, header_paths)
            if record:
                page_records.append(record)

        if not page_records:
            raise RuntimeError(f"第 {page_no} 页存在表格行，但没有解析出域名。")

        signature = "\n".join(
            f"{record.host}\t{record.info}" for record in page_records
        )
        if signature in seen_page_signatures:
            raise RuntimeError("检测到分页 DOM 未更新，停止以避免无限重复抓取。")
        seen_page_signatures.add(signature)

        all_records.extend(page_records)
        print(
            f"[网页] 第 {page_no} 页：{len(page_records)} 条，累计 {len(all_records)} 条"
        )

        next_button = page.locator("button.btn-next").first
        if not next_button.count():
            # 某些版本可能把 next 放在 li 上。
            next_button = page.locator(
                ".el-pagination .btn-next, .el-pager + button.btn-next"
            ).first

        if not next_button.count():
            break

        disabled = next_button.is_disabled()
        if disabled:
            break

        first_row_text = rows.nth(0).inner_text()
        next_button.click()

        try:
            page.wait_for_function(
                """(oldText) => {
                    const rows = document.querySelectorAll(
                        '.el-table__body-wrapper tbody tr.el-table__row,' +
                        '.el-table__body tbody tr.el-table__row,' +
                        'table tbody tr.el-table__row,' +
                        '.el-table__row'
                    );
                    if (!rows.length) return false;
                    return rows[0].innerText !== oldText;
                }""",
                first_row_text,
                timeout=TABLE_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
            # 再等一次，若行文本仍然没变，就按失败处理。
            page.wait_for_timeout(1000)
            current_rows = find_data_rows(page)
            if current_rows.count() == 0:
                raise RuntimeError("翻页后没有数据行。")
            if current_rows.nth(0).inner_text() == first_row_text:
                raise RuntimeError("点击下一页后表格内容没有变化。")

        page.wait_for_timeout(DOM_SETTLE_MS)

    return all_records


def scrape_vps789_page(page_url: str) -> list[DomainRecord]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=USER_AGENT,
            ignore_https_errors=False,
            viewport={"width": 1440, "height": 1000},
            locale="zh-CN",
        )
        page = context.new_page()
        page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
        page.set_default_timeout(TABLE_TIMEOUT_MS)

        try:
            last_error: Exception | None = None
            for attempt in range(1, 4):
                try:
                    print(f"[网页] 打开 VPS789（尝试 {attempt}/3）")
                    page.goto(
                        page_url,
                        wait_until="domcontentloaded",
                        timeout=NAVIGATION_TIMEOUT_MS,
                    )
                    return collect_page_domains(page)
                except Exception as exc:
                    last_error = exc
                    print(f"[网页] 尝试 {attempt} 失败：{exc}")
                    if attempt < 3:
                        page.reload(
                            wait_until="domcontentloaded",
                            timeout=NAVIGATION_TIMEOUT_MS,
                        )
                        time.sleep(1)

            raise RuntimeError(f"VPS789 网页抓取失败：{last_error}") from last_error
        finally:
            browser.close()


def fetch_top20_api() -> list[DomainRecord]:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    }

    try:
        with httpx.Client(
            timeout=httpx.Timeout(20.0, connect=10.0),
            follow_redirects=True,
            headers=headers,
        ) as client:
            response = client.get(TOP20_API_URL)
            response.raise_for_status()
            payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        print(f"[API] 请求失败，跳过 API 补充：{exc}")
        return []

    if not isinstance(payload, dict):
        print("[API] 返回内容不是 JSON 对象，跳过 API 补充。")
        return []

    if payload.get("code") not in (0, None):
        print(f"[API] 接口返回异常：code={payload.get('code')}")
        return []

    good = ((payload.get("data") or {}).get("good") or [])
    if not isinstance(good, list):
        print("[API] data.good 不是数组，跳过 API 补充。")
        return []

    records: list[DomainRecord] = []
    seen: set[str] = set()

    for item in good:
        if not isinstance(item, dict):
            continue

        raw_host = item.get("ip")
        host = display_host(str(raw_host or ""))
        if not is_plausible_host(host):
            continue

        latency = format_number(item.get("dxLatency"))
        loss = format_number(item.get("dxPkgLostRate"))

        # Top20 JSON 当前没有下载速度字段，因此速度留空。
        info = format_info(latency=latency, loss=loss, speed="")

        record = DomainRecord(host=host, info=info)
        if record.key in seen:
            continue

        seen.add(record.key)
        records.append(record)

    print(f"[API] 解析得到 {len(records)} 个有效域名。")
    return records


def merge_sources(
    web_records: list[DomainRecord],
    api_records: list[DomainRecord],
    target_count: int,
) -> list[DomainRecord]:
    if len(web_records) >= target_count:
        return web_records[:target_count]

    # 这里严格按用户要求：
    # 1. 网页源先完整抓取，网页自身不去重
    # 2. API 源再与网页源做完全相同域名比较
    # 3. API 里重复的只保留一份
    web_keys = {record.key for record in web_records}
    api_seen: set[str] = set()

    merged = list(web_records)

    for record in api_records:
        if record.key in web_keys or record.key in api_seen:
            continue
        api_seen.add(record.key)
        merged.append(record)
        if len(merged) >= target_count:
            break

    return merged


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    tmp.replace(path)


def write_outputs(records: list[DomainRecord], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    for port in PORTS:
        lines = [
            f"{record.host}:{port}#{record.info}"
            for record in records
        ]
        content = "\n".join(lines) + ("\n" if lines else "")
        output_path = output_dir / f"{port}.txt"
        atomic_write(output_path, content)
        print(f"[输出] {output_path} -> {len(lines)} 行")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抓取 VPS789 优选域名，叠加 Top20 API 去重补充，并生成六个端口文件。"
    )
    parser.add_argument(
        "--count",
        type=int,
        default=TARGET_COUNT,
        help=f"最终输出域名数量，默认 {TARGET_COUNT}。",
    )
    parser.add_argument(
        "--min-count",
        type=int,
        default=MIN_COUNT,
        help=f"最低要求数量，默认 {MIN_COUNT}。",
    )
    parser.add_argument(
        "--url",
        default=PAGE_URL,
        help="VPS789 域名页面地址。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.count < 1:
        print("--count 必须大于 0。", file=sys.stderr)
        return 2
    if args.min_count < 1 or args.min_count > args.count:
        print("--min-count 必须满足 1 <= min-count <= count。", file=sys.stderr)
        return 2

    output_dir = Path(__file__).resolve().parent

    print("=== VPS789 优选域名聚合开始 ===")
    print(f"目标数量：{args.count}")
    print(f"输出目录：{output_dir}")
    print(f"端口：{', '.join(map(str, PORTS))}")

    web_records = scrape_vps789_page(args.url)
    print(f"[网页] 完整抓取：{len(web_records)} 条（网页源自身不去重）")

    api_records = fetch_top20_api()

    merged = merge_sources(
        web_records=web_records,
        api_records=api_records,
        target_count=args.count,
    )

    print(
        f"[合并] 网页 {len(web_records)} + API补充后 {len(merged)} 条"
    )

    if len(merged) < args.min_count:
        raise RuntimeError(
            f"有效域名不足 {args.min_count} 条：当前仅 {len(merged)} 条。"
        )

    # 即便网页已有 300+ 条，默认只输出前 200 条。
    merged = merged[: args.count]
    write_outputs(merged, output_dir)

    print("=== VPS789 优选域名聚合完成 ===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
