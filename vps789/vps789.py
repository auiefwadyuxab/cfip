#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VPS789 优选域名聚合器。

目标：
1. 从 VPS789 域名页抓取全部分页，严格保持网页顺序，网页源本身不去重、不截断。
2. 只读取每行的：CF 优选域名、 电信(24H) 延迟/丢包率、 下载速度。
3. 延迟输出保留 `ms`，例如：`68ms/0-13.5MB/s`。
4. 再读取 VPS789 Top20 API；API 只补充网页源不存在的域名，API 自身去重。
5. 生成 443/8443/2053/2083/2087/2096 六个端口文件，域名顺序完全一致。

网页字段示例：
    CF优选IP/域名 | 电信(24H) | 移动(24H) | 联通(24H) | 下载速度 | 评分 | 备注 | ...

输出格式：
    域名:端口#电信延迟/电信丢包率-下载速度

示例：
    example.com:443#68ms/0-13.5MB/s
    example.com:8443#68ms/0-13.5MB/s
    API-only.example:443#82ms/0-
"""

from __future__ import annotations

import argparse
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from playwright.sync_api import Locator, Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


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

# VPS789 当前页面使用 ElementPlus 表格。按优先级排列，避免固定列/普通 body
# 与备用 DOM 同时存在时错误地抓到重复行。
DATA_ROW_SELECTORS = (
    ".el-table__body-wrapper tbody tr.el-table__row",
    ".el-table__body tbody tr.el-table__row",
    "table tbody tr.el-table__row",
    ".el-table__body-wrapper tbody tr",
    ".el-table__body tbody tr",
    "table tbody tr",
)

# 主机名/IPv4/常见主机字符串。remarks=domain 页面通常返回 ASCII 域名。
HOST_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?)$"
)

# 电信单元格的实际显示格式，例如：68ms/0%、106ms/0.21%。
TELECOM_METRIC_RE = re.compile(
    r"(?P<latency>-?\d+(?:\.\d+)?)\s*ms\s*/\s*"
    r"(?P<loss>-?\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)

# 下载速度单元格的实际显示格式，例如：13.5MB/s、12.1 MB/s。
DOWNLOAD_SPEED_RE = re.compile(
    r"(?P<number>-?\d+(?:\.\d+)?)\s*"
    r"(?P<unit>KB|MB|GB|TB)\s*/\s*s\b",
    re.IGNORECASE,
)

NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass(frozen=True)
class DomainRecord:
    host: str
    info: str = ""
    source: Literal["web", "api"] = "web"

    @property
    def key(self) -> str:
        return normalize_host(self.host)


def clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def normalize_host(value: str) -> str:
    """用于来源之间的域名比较，不改变实际输出域名顺序。"""
    value = clean_text(value)
    value = re.sub(r"(?i)^https?://", "", value)
    value = value.split("/", 1)[0]
    return value.rstrip(".").casefold()


def display_host(value: str) -> str:
    value = clean_text(value)
    value = re.sub(r"(?i)^https?://", "", value)
    value = value.split("/", 1)[0]
    return value.rstrip(".")


def is_plausible_host(value: str) -> bool:
    host = display_host(value)
    return bool(host and " " not in host and HOST_RE.fullmatch(host))


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


def format_info(latency: str = "", loss: str = "", speed: str = "") -> str:
    if not any((latency, loss, speed)):
        return ""
    latency_part = f"{latency}ms" if latency else ""
    return f"{latency_part}/{loss}-{speed}"


def get_row_cells(row: Locator) -> list[str]:
    """读取一行所有 td 文本；重绘瞬间为空时用 text_content() 回退。"""
    td_locator = row.locator("td")
    try:
        cells = [clean_text(value) for value in td_locator.all_inner_texts()]
    except Exception:
        cells = []

    if cells and any(cells):
        return cells

    return [
        clean_text(td_locator.nth(i).text_content() or "")
        for i in range(td_locator.count())
    ]


def find_data_rows(page: Page) -> Locator:
    """只返回当前可见数据表体中优先级最高的一组行。"""
    for selector in DATA_ROW_SELECTORS:
        locator = page.locator(selector)
        if locator.count() > 0:
            return locator
    return page.locator("table tbody tr")


def wait_for_first_data_row(page: Page) -> Locator:
    """等待异步表格真正出现数据。"""
    rows = page.locator(",".join(DATA_ROW_SELECTORS))
    first_row = rows.first
    first_row.wait_for(state="attached", timeout=TABLE_TIMEOUT_MS)

    # 使用无参数的 Page.wait_for_function，避免把外部字符串拼进 JS，
    # 从根源上规避前一版的 SyntaxError，同时兼容 Playwright 1.57+。
    page.wait_for_function(
        "() => { const row = document.querySelector("
        "'.el-table__body-wrapper tbody tr.el-table__row, "
        ".el-table__body tbody tr.el-table__row, "
        "table tbody tr.el-table__row, "
        ".el-table__body-wrapper tbody tr, "
        ".el-table__body tbody tr, "
        "table tbody tr'"
        "); return !!row && row.innerText.trim().length > 0; }",
        polling=100,
        timeout=TABLE_TIMEOUT_MS,
    )
    return first_row


def extract_domain_from_cells(cells: list[str]) -> tuple[int, str] | None:
    """优先读取第一列；若页面加入辅助列，再顺序寻找合法域名。"""
    if cells:
        first = display_host(cells[0])
        if is_plausible_host(first) and "." in first:
            return 0, first

    for index, cell in enumerate(cells):
        candidate = display_host(cell)
        if is_plausible_host(candidate) and "." in candidate:
            return index, candidate

    for index, cell in enumerate(cells):
        candidate = display_host(cell)
        if is_plausible_host(candidate):
            return index, candidate

    return None


def extract_telecom_metric(cells: list[str]) -> tuple[str, str, int]:
    """同一行中第一个 `ms/%` 指标就是电信(24H)。"""
    for index, text in enumerate(cells):
        match = TELECOM_METRIC_RE.search(text)
        if match:
            return (
                format_number(match.group("latency")),
                format_number(match.group("loss")),
                index,
            )
    return "", "", -1


def extract_download_speed(cells: list[str], telecom_index: int) -> str:
    """读取下载速度，优先使用截图对应的第五列，再做语义回退。"""
    candidates: list[str] = []

    # 当前页面结构：0=域名，1=电信，2=移动，3=联通，4=下载速度。
    if len(cells) > 4:
        candidates.append(cells[4])

    start = telecom_index + 1 if telecom_index >= 0 else 0
    candidates.extend(cells[start:])

    seen_text: set[str] = set()
    for text in candidates:
        if text in seen_text:
            continue
        seen_text.add(text)
        match = DOWNLOAD_SPEED_RE.search(text)
        if match:
            number = format_number(match.group("number"))
            unit = match.group("unit").upper()
            return f"{number}{unit}/s"
    return ""


def extract_record_from_row(cells: list[str]) -> DomainRecord | None:
    domain_result = extract_domain_from_cells(cells)
    if domain_result is None:
        return None

    _, host = domain_result
    latency, loss, telecom_index = extract_telecom_metric(cells)
    if telecom_index < 0:
        return None

    speed = extract_download_speed(cells, telecom_index)
    if not speed:
        return None

    return DomainRecord(
        host=host,
        info=format_info(latency=latency, loss=loss, speed=speed),
        source="web",
    )


def validate_web_record(record: DomainRecord) -> bool:
    """网页源必须有完整的电信延迟/丢包率与下载速度。"""
    return bool(
        record.source == "web"
        and re.fullmatch(
            r"\d+(?:\.\d+)?ms/\d+(?:\.\d+)?-\d+(?:\.\d+)?(KB|MB|GB|TB)/s",
            record.info,
            re.IGNORECASE,
        )
    )


def get_pagination_active_locator(page: Page) -> Locator:
    return page.locator(
        ".el-pagination .el-pager li.number.active, "
        ".el-pagination .el-pager li.number.is-active"
    ).first


def wait_for_page_change(
    page: Page,
    rows: Locator,
    old_first_text: str,
    old_page_number: str,
) -> None:
    """优先等待页码变化，退回到首行变化。

    外部文本先通过 DOM dataset 写入，再使用无参数 JS predicate，
    从而不把 Python 字符串直接嵌入 JavaScript 源代码。
    """
    active_page = get_pagination_active_locator(page)
    if active_page.count() > 0 and old_page_number:
        try:
            page.wait_for_function(
                "() => { const el = document.querySelector("
                "'.el-pagination .el-pager li.number.active, .el-pagination .el-pager li.number.is-active'"
                "); return !!el && el.innerText.trim() !== document.documentElement.dataset.vps789OldPage; }",
                polling=100,
                timeout=PAGINATION_TIMEOUT_MS,
            )
            return
        except PlaywrightTimeoutError:
            pass

    try:
        page.wait_for_function(
            "() => { const rows = document.querySelectorAll("
            "'.el-table__body-wrapper tbody tr.el-table__row, .el-table__body tbody tr.el-table__row, table tbody tr.el-table__row, table tbody tr'"
            "); if (!rows.length) return false; const signature = Array.from(rows).slice(0, 2).map(row => row.innerText.trim()).join('\u001f'); return signature.length > 0 && signature !== document.documentElement.dataset.vps789OldFirst; }",
            polling=100,
            timeout=PAGINATION_TIMEOUT_MS,
        )
    except PlaywrightTimeoutError as exc:
        raise RuntimeError("点击下一页后表格内容没有确认发生变化。") from exc


def collect_page_domains(page: Page) -> list[DomainRecord]:
    wait_for_first_data_row(page)

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
        for index in range(row_count):
            row = rows.nth(index)
            cells = get_row_cells(row)
            record = extract_record_from_row(cells)
            if record is not None:
                page_records.append(record)

        if not page_records:
            raise RuntimeError(
                f"第 {page_no} 页存在数据行，但没有解析出完整的域名/电信/下载字段。"
            )

        incomplete = [record.host for record in page_records if not validate_web_record(record)]
        if incomplete:
            sample = ", ".join(incomplete[:5])
            raise RuntimeError(
                f"第 {page_no} 页存在格式不完整的数据：{len(incomplete)} 条；示例：{sample}"
            )

        # 仅用于检测分页卡死；不参与网页源域名去重。
        signature = "\n".join(f"{record.host}\t{record.info}" for record in page_records)
        if signature in seen_page_signatures:
            raise RuntimeError("检测到分页内容重复，疑似分页没有成功切换。")
        seen_page_signatures.add(signature)

        all_records.extend(page_records)
        print(
            f"[网页] 第 {page_no} 页：{len(page_records)} 条，"
            f"信息完整 {len(page_records)}/{len(page_records)} 条，"
            f"累计 {len(all_records)} 条"
        )

        next_button = page.locator("button.btn-next").first
        if next_button.count() == 0:
            next_button = page.locator(
                ".el-pagination button.btn-next, .el-pagination .btn-next"
            ).first

        if next_button.count() == 0:
            break

        if next_button.is_disabled():
            break

        first_row = rows.first
        old_first_text = "\u001f".join(
            clean_text(rows.nth(i).inner_text()) for i in range(min(2, row_count))
        )
        active_page = get_pagination_active_locator(page)
        old_page_number = clean_text(active_page.inner_text()) if active_page.count() else ""

        # 保存比较基准；翻页后的等待完全基于当前 DOM 状态。
        page.evaluate(
            "value => document.documentElement.dataset.vps789OldPage = value",
            old_page_number,
        )
        page.evaluate(
            "value => document.documentElement.dataset.vps789OldFirst = value",
            old_first_text,
        )

        next_button.click()
        wait_for_page_change(page, rows, old_first_text, old_page_number)

    return all_records


def build_debug_paths(output_dir: Path, attempt: int) -> tuple[Path, Path]:
    debug_dir = output_dir / ".diagnostics"
    debug_dir.mkdir(parents=True, exist_ok=True)
    return (
        debug_dir / f"failure-attempt-{attempt}.png",
        debug_dir / f"failure-attempt-{attempt}.html",
    )


def capture_page_debug(page: Page, output_dir: Path, attempt: int) -> None:
    """失败时保存页面快照，交给 Actions artifact，不进入 git add。"""
    image_path, html_path = build_debug_paths(output_dir, attempt)
    try:
        page.screenshot(path=str(image_path), full_page=True)
    except Exception as exc:
        print(f"[诊断] 页面截图保存失败：{exc}")
    try:
        html_path.write_text(page.content(), encoding="utf-8")
    except Exception as exc:
        print(f"[诊断] HTML 保存失败：{exc}")


def new_page(browser):
    context = browser.new_context(
        user_agent=USER_AGENT,
        ignore_https_errors=False,
        viewport={"width": 1440, "height": 1000},
        locale="zh-CN",
    )
    page = context.new_page()
    page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)
    page.set_default_timeout(TABLE_TIMEOUT_MS)
    return context, page


def scrape_vps789_page(page_url: str, output_dir: Path) -> list[DomainRecord]:
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            last_error: Exception | None = None

            for attempt in range(1, RETRY_COUNT + 1):
                context = None
                page = None
                try:
                    print(f"[网页] 打开 VPS789（尝试 {attempt}/{RETRY_COUNT}）")
                    context, page = new_page(browser)
                    page.goto(
                        page_url,
                        wait_until="domcontentloaded",
                        timeout=NAVIGATION_TIMEOUT_MS,
                    )
                    records = collect_page_domains(page)
                    if not records:
                        raise RuntimeError("VPS789 网页没有抓到任何域名。")
                    return records
                except Exception as exc:
                    last_error = exc
                    print(f"[网页] 尝试 {attempt} 失败：{exc}")
                    if page is not None:
                        capture_page_debug(page, output_dir, attempt)
                    if attempt < RETRY_COUNT:
                        # 指数退避，但不使用固定长时间 sleep。
                        time.sleep(min(2 ** (attempt - 1), 4))
                finally:
                    if context is not None:
                        context.close()

            raise RuntimeError(f"VPS789 网页抓取失败：{last_error}") from last_error
        finally:
            browser.close()


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

            for item in good:
                if not isinstance(item, dict):
                    continue

                host = display_host(str(item.get("ip") or ""))
                if not is_plausible_host(host):
                    continue

                latency = format_number(item.get("dxLatency"))
                loss = format_number(item.get("dxPkgLostRate"))
                if not latency or not loss:
                    continue

                record = DomainRecord(
                    host=host,
                    info=format_info(latency=latency, loss=loss, speed=""),
                    source="api",
                )

                if record.key in seen:
                    continue
                seen.add(record.key)
                records.append(record)

            print(f"[API] 解析得到 {len(records)} 个有效域名。")
            return records

        except (httpx.HTTPError, ValueError) as exc:
            last_error = exc
            print(f"[API] 请求失败（尝试 {attempt}/{API_RETRY_COUNT}）：{exc}")
            if attempt < API_RETRY_COUNT:
                time.sleep(min(2 ** (attempt - 1), 4))

    print(f"[API] {API_RETRY_COUNT} 次请求均失败，跳过 API 补充：{last_error}")
    return []


def merge_sources(
    web_records: list[DomainRecord],
    api_records: list[DomainRecord],
) -> list[DomainRecord]:
    """网页优先、网页不去重、API 只补充不存在域名。"""
    merged = list(web_records)
    existing_keys = {record.key for record in web_records}
    api_added = 0

    for record in api_records:
        if record.key in existing_keys:
            continue
        existing_keys.add(record.key)
        merged.append(record)
        api_added += 1

    print(
        f"[合并] 网页源 {len(web_records)} 条（原样保留） + "
        f"API 新增 {api_added} 条 = {len(merged)} 条"
    )
    return merged


def atomic_write(path: Path, content: str) -> None:
    tmp = path.with_name(f".{path.name}.{Path.cwd().name}.tmp")
    try:
        tmp.write_text(content, encoding="utf-8", newline="\n")
        tmp.replace(path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def write_outputs(records: list[DomainRecord], output_dir: Path) -> None:
    """六个文件使用相同记录列表，只改变端口。"""
    output_dir.mkdir(parents=True, exist_ok=True)

    rendered: dict[int, str] = {}
    for port in PORTS:
        lines = [f"{record.host}:{port}#{record.info}" for record in records]
        rendered[port] = "\n".join(lines) + ("\n" if lines else "")

    # 先全部写入临时文件，再统一替换，避免中途异常导致输出文件处于空文件状态。
    temp_paths: list[tuple[Path, Path]] = []
    try:
        for port, content in rendered.items():
            output_path = output_dir / f"{port}.txt"
            temp_path = output_dir / f".{port}.txt.vps789.tmp"
            temp_path.write_text(content, encoding="utf-8", newline="\n")
            temp_paths.append((temp_path, output_path))

        for temp_path, output_path in temp_paths:
            temp_path.replace(output_path)
            print(f"[输出] {output_path} -> {len(records)} 行")
    finally:
        for temp_path, _ in temp_paths:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass


def cleanup_diagnostics(output_dir: Path) -> None:
    """成功后删除旧诊断文件，避免 artifact 带入无关历史数据。"""
    debug_dir = output_dir / ".diagnostics"
    if not debug_dir.exists():
        return
    for path in debug_dir.iterdir():
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass
    try:
        debug_dir.rmdir()
    except OSError:
        pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "抓取 VPS789 全部网页分页；读取 CF 优选域名、电信24H延迟/丢包率、下载速度；"
            "再用 Top20 API 去重补充，并生成六个端口文件。"
        )
    )
    parser.add_argument("--url", default=PAGE_URL, help="VPS789 域名页面地址。")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    output_dir = Path(__file__).resolve().parent

    print("=== VPS789 优选域名聚合开始 ===")
    print("网页源：全部分页，数量不做截断，网页源自身不去重")
    print("API 源：Top20，仅补充网页源中不存在的域名，并对 API 自身去重")
    print("网页字段：CF优选IP/域名 + 电信(24H) 延迟/丢包率 + 下载速度")
    print("输出格式：域名:端口#延迟ms/丢包率-下载速度")
    print(f"输出目录：{output_dir}")
    print(f"端口：{', '.join(map(str, PORTS))}")

    web_records = scrape_vps789_page(args.url, output_dir)
    if not web_records:
        raise RuntimeError("VPS789 网页没有抓到任何域名。")

    invalid_count = sum(not validate_web_record(record) for record in web_records)
    if invalid_count:
        raise RuntimeError(f"网页源有 {invalid_count} 条记录未通过完整字段验证。")

    print(
        f"[网页] 完整抓取：{len(web_records)} 条；"
        f"电信延迟/丢包率 + 下载速度完整：{len(web_records)}/{len(web_records)} 条"
    )

    api_records = fetch_top20_api()
    merged = merge_sources(web_records=web_records, api_records=api_records)

    write_outputs(merged, output_dir)
    cleanup_diagnostics(output_dir)

    print("=== VPS789 优选域名聚合完成 ===")
    print(f"最终输出域名：{len(merged)} 条（无数量上限）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
