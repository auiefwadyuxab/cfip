#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VPS789 优选域名聚合器（全部分页 + 电信指标 + 下载速度）

数据源：
1. VPS789 动态优选域名页：全部分页，保留网页原始顺序，不在脚本中截断
   https://vps789.com/cfip/?remarks=domain
2. VPS789 官方 Top20 API：通常每日刷新；仅补充网页源不存在的域名
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

import httpx
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


PAGE_URL = "https://vps789.com/cfip/?remarks=domain"
TOP20_API_URL = "https://vps789.com/openApi/cfIpTop20"

PORTS = (443, 8443, 2053, 2083, 2087, 2096)

NAVIGATION_TIMEOUT_MS = 60_000
TABLE_TIMEOUT_MS = 30_000
DOM_SETTLE_MS = 800

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/151.0.0.0 Safari/537.36"
)

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

TELECOM_METRIC_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*ms\s*/\s*(-?\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
DOWNLOAD_SPEED_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*(KB|MB|GB|TB)\s*/\s*s\b",
    re.IGNORECASE,
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


def format_info(latency: str = "", loss: str = "", speed: str = "") -> str:
    """
    严格保持：
        延迟/丢包率-下载速度
    速度没有时保留末尾 '-'，例如 78/0-
    """
    if not any((latency, loss, speed)):
        return ""
    return f"{latency}/{loss}-{speed}"


def get_row_cells(row: Locator) -> list[str]:
    """读取当前行所有 td 的可见文本；VPS789 的指标文本就在表格 DOM 中。"""
    try:
        values = [clean_text(value) for value in row.locator("td").all_inner_texts()]
    except Exception:
        values = []

    # 极少数重绘情况下 innerText 可能暂时为空，textContent 作为一次轻量回退。
    if not any(values):
        td_locator = row.locator("td")
        values = [
            clean_text(td_locator.nth(i).text_content() or "")
            for i in range(td_locator.count())
        ]

    return values


def extract_domain_from_cells(cells: list[str]) -> tuple[int, str] | None:
    """
    找到第一个看起来像域名/主机名的单元格。
    页面当前第一列就是 CF 优选 IP/域名，因此通常直接命中；
    顺序扫描可兼容前面出现序号、选择框等辅助列。
    """
    for index, cell in enumerate(cells):
        candidate = display_host(cell)
        if not is_plausible_host(candidate):
            continue
        if "." in candidate:
            return index, candidate

    for index, cell in enumerate(cells):
        candidate = display_host(cell)
        if is_plausible_host(candidate):
            return index, candidate

    return None


def extract_telecom_metric(cells: list[str]) -> tuple[str, str]:
    """
    页面三网指标依次为：电信、移动、联通。
    不依赖复杂的多行/合并表头，而是识别单元格本身的“延迟/丢包率”文本：
    同一行中第一个匹配项就是电信(24H)。
    """
    for text in cells:
        match = TELECOM_METRIC_RE.search(text)
        if match:
            return (
                format_number(match.group(1)),
                format_number(match.group(2)),
            )
    return "", ""


def extract_download_speed(cells: list[str]) -> str:
    """
    只读取“下载速度”列。
    页面当前下载速度直接带 KB/s、MB/s、GB/s 等单位，因此识别速度单位
    比按固定列号更稳健，也不会误把移动/联通的数字当成下载速度。
    """
    for text in cells:
        match = DOWNLOAD_SPEED_RE.search(text)
        if match:
            return f"{format_number(match.group(1))}{match.group(2).upper()}/s"
    return ""


def extract_record_from_row(
    cells: list[str],
) -> DomainRecord | None:
    if not cells:
        return None

    domain_result = extract_domain_from_cells(cells)
    if domain_result is None:
        return None

    _, host = domain_result
    latency, loss = extract_telecom_metric(cells)
    speed = extract_download_speed(cells)

    return DomainRecord(
        host=host,
        info=format_info(latency=latency, loss=loss, speed=speed),
    )


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


def collect_page_domains(page: Page) -> list[DomainRecord]:
    page.wait_for_selector(
        ".el-table__row, table tbody tr, .el-table__body-wrapper tbody tr",
        timeout=TABLE_TIMEOUT_MS,
    )
    time.sleep(DOM_SETTLE_MS / 1000)

    all_records: list[DomainRecord] = []
    seen_page_signatures: set[str] = set()

    page_no = 0
    while True:
        page_no += 1
        rows = find_data_rows(page)
        row_count = rows.count()

        if row_count == 0:
            raise RuntimeError(f"第 {page_no} 页没有找到数据行。")

        page_records: list[DomainRecord] = []
        for i in range(row_count):
            row = rows.nth(i)
            cells = get_row_cells(row)
            record = extract_record_from_row(cells)
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

        info_count = sum(bool(record.info) for record in page_records)
        print(
            f"[网页] 第 {page_no} 页：{len(page_records)} 条，"
            f"信息完整 {info_count}/{len(page_records)} 条，"
            f"累计 {len(all_records)} 条"
        )

        next_button = page.locator("button.btn-next").first
        if not next_button.count():
            next_button = page.locator(
                ".el-pagination .btn-next, .el-pager + button.btn-next"
            ).first

        if not next_button.count() or next_button.is_disabled():
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
                arg=first_row_text,
                timeout=TABLE_TIMEOUT_MS,
            )
        except PlaywrightTimeoutError:
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
) -> list[DomainRecord]:
    """
    保持原来的来源优先级：
    1. 网页源全部保留，网页源本身不去重、不截断。
    2. API 源逐个与“已经存在的网页域名 + 已加入的 API 域名”比较。
    3. 已存在则跳过；不存在则追加。
    最终数量完全由两个来源实际返回的数据量决定。
    """
    merged = list(web_records)
    seen_keys = {record.key for record in web_records}

    added = 0
    for record in api_records:
        if record.key in seen_keys:
            continue
        seen_keys.add(record.key)
        merged.append(record)
        added += 1

    print(
        f"[合并] 网页源 {len(web_records)} 条（原样保留） + "
        f"API 新增 {added} 条 = {len(merged)} 条"
    )
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
        description="抓取 VPS789 优选域名全部分页，读取电信延迟/丢包率与下载速度，"
        "再用 Top20 API 去重补充，并生成六个端口文件。"
    )
    parser.add_argument(
        "--url",
        default=PAGE_URL,
        help="VPS789 域名页面地址。",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(__file__).resolve().parent

    print("=== VPS789 优选域名聚合开始 ===")
    print("网页源：全部分页，数量不做截断，网页源自身不去重")
    print("API 源：Top20，仅补充网页源中不存在的域名")
    print(f"输出目录：{output_dir}")
    print(f"端口：{', '.join(map(str, PORTS))}")

    web_records = scrape_vps789_page(args.url)
    complete_web_info = sum(bool(record.info) for record in web_records)
    print(
        f"[网页] 完整抓取：{len(web_records)} 条；"
        f"电信+下载信息完整：{complete_web_info}/{len(web_records)} 条"
    )

    if not web_records:
        raise RuntimeError("VPS789 网页没有抓到任何域名。")

    # 防止页面结构变化导致“域名全抓到、指标全空”却静默生成错误文件。
    # 至少有一条完整记录即可继续；若整页全部缺指标则直接报出结构问题。
    if complete_web_info == 0:
        raise RuntimeError(
            "网页域名抓取成功，但电信延迟/丢包率与下载速度全部为空，"
            "请检查 VPS789 页面结构是否发生变化。"
        )

    api_records = fetch_top20_api()
    merged = merge_sources(web_records=web_records, api_records=api_records)

    write_outputs(merged, output_dir)

    print("=== VPS789 优选域名聚合完成 ===")
    print(f"最终输出域名：{len(merged)} 条（无数量上限）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
