#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""VPS789 优选域名聚合器。

抓取 VPS789 域名页的全部分页，并用 Top20 API 补充网页未出现的域名。
网页源保持原始顺序且不去重；API 自身去重，并且只补充网页源没有的域名。
输出 443/8443/2053/2083/2087/2096 六个端口文件，以及按端口顺序
拼接六个文件内容的 all.txt。所有文件使用相同的节点顺序和测速信息。

输出格式：域名:端口#电信延迟ms/电信丢包率-下载速度
例如：example.com:443#68ms/0-13.5MB/s
"""

from __future__ import annotations

import argparse
import re
import time
from dataclasses import dataclass
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
HOST_RE = re.compile(
    r"^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?)$"
)
TELECOM_METRIC_RE = re.compile(
    r"(?P<latency>-?\d+(?:\.\d+)?)\s*ms\s*/\s*"
    r"(?P<loss>-?\d+(?:\.\d+)?)\s*%",
    re.IGNORECASE,
)
DOWNLOAD_SPEED_RE = re.compile(
    r"(?P<number>-?\d+(?:\.\d+)?)\s*(?P<unit>KB|MB|GB|TB)\s*/\s*s\b",
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
    """仅用于不同来源之间比较域名，不改变实际输出的主机名。"""
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


def get_row_cells(row: Locator) -> list[str]:
    """读取表格一行所有单元格；瞬时空值时回退到 text_content。"""
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
    for selector in DATA_ROW_SELECTORS:
        locator = page.locator(selector)
        if locator.count() > 0:
            return locator
    return page.locator("table tbody tr")


def row_signature(rows: Locator, limit: int = 2) -> tuple[str, ...]:
    return tuple(
        clean_text(rows.nth(i).inner_text())
        for i in range(min(rows.count(), limit))
    )


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
        return rows.count() > 0 and any(row_signature(rows, limit=2))

    if not wait_until(deadline, ready):
        raise RuntimeError("等待 VPS789 数据表超时：页面没有出现有效数据行。")
    return rows


def extract_domain_from_cells(cells: list[str]) -> tuple[int, str] | None:
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
    for index, cell in enumerate(cells):
        match = TELECOM_METRIC_RE.search(cell)
        if match:
            return (
                format_number(match.group("latency")),
                format_number(match.group("loss")),
                index,
            )
    return "", "", -1


def extract_download_speed(cells: list[str], telecom_index: int) -> str:
    candidates: list[str] = []
    # 当前页面结构：0=域名，1=电信，2=移动，3=联通，4=下载速度。
    if len(cells) > 4:
        candidates.append(cells[4])
    start = telecom_index + 1 if telecom_index >= 0 else 0
    candidates.extend(cells[start:])
    seen: set[str] = set()
    for cell in candidates:
        if cell in seen:
            continue
        seen.add(cell)
        match = DOWNLOAD_SPEED_RE.search(cell)
        if match:
            return f"{format_number(match.group('number'))}{match.group('unit').upper()}/s"
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
    return DomainRecord(host=host, info=format_info(latency, loss, speed), source="web")


def validate_web_record(record: DomainRecord) -> bool:
    return bool(
        record.source == "web"
        and re.fullmatch(
            r"\d+(?:\.\d+)?ms/\d+(?:\.\d+)?-\d+(?:\.\d+)?(KB|MB|GB|TB)/s",
            record.info,
            re.IGNORECASE,
        )
    )


def get_active_page_locator(page: Page) -> Locator:
    return page.locator(
        ".el-pagination .el-pager li.number.active, "
        ".el-pagination .el-pager li.number.is-active"
    ).first


def wait_for_page_change(
    page: Page, rows: Locator, old_signature: str, old_page_number: str
) -> None:
    deadline = time.monotonic() + PAGINATION_TIMEOUT_MS / 1000

    def changed() -> bool:
        active = get_active_page_locator(page)
        current_page = clean_text(active.inner_text()) if active.count() else ""
        current_signature = "\x1f".join(row_signature(rows, limit=2))
        if current_page and old_page_number:
            return current_page != old_page_number and current_signature != old_signature and bool(current_signature)
        return current_signature != old_signature and bool(current_signature)

    if not wait_until(deadline, changed):
        raise RuntimeError("点击下一页后表格内容没有确认发生变化。")


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
            record = extract_record_from_row(get_row_cells(rows.nth(index)))
            if record is not None:
                page_records.append(record)

        if not page_records:
            raise RuntimeError(f"第 {page_no} 页没有解析出完整的域名/电信/下载字段。")

        incomplete = [r.host for r in page_records if not validate_web_record(r)]
        if incomplete:
            raise RuntimeError(
                f"第 {page_no} 页存在 {len(incomplete)} 条格式不完整的数据；"
                f"示例：{', '.join(incomplete[:5])}"
            )

        signature = "\n".join(f"{r.host}\t{r.info}" for r in page_records)
        if signature in seen_page_signatures:
            raise RuntimeError("检测到分页内容重复，疑似分页没有成功切换。")
        seen_page_signatures.add(signature)
        all_records.extend(page_records)
        print(f"[网页] 第 {page_no} 页：{len(page_records)} 条，累计 {len(all_records)} 条")

        next_button = page.locator("button.btn-next").first
        if next_button.count() == 0:
            next_button = page.locator(
                ".el-pagination button.btn-next, .el-pagination .btn-next"
            ).first
        if next_button.count() == 0 or next_button.is_disabled():
            break

        old_signature = "\x1f".join(row_signature(rows, limit=2))
        active = get_active_page_locator(page)
        old_page_number = clean_text(active.inner_text()) if active.count() else ""
        next_button.click()
        wait_for_page_change(page, rows, old_signature, old_page_number)

    return all_records


def capture_page_debug(page: Page, output_dir: Path, attempt: int) -> None:
    debug_dir = output_dir / ".diagnostics"
    debug_dir.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(debug_dir / f"failure-attempt-{attempt}.png"), full_page=True)
    except Exception as exc:
        print(f"[诊断] 页面截图保存失败：{exc}")
    try:
        (debug_dir / f"failure-attempt-{attempt}.html").write_text(
            page.content(), encoding="utf-8"
        )
    except Exception as exc:
        print(f"[诊断] HTML 保存失败：{exc}")


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
                    page.goto(page_url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
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
                latency = parse_metric_number(item.get("dxLatency"))
                loss = parse_metric_number(item.get("dxPkgLostRate"))
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
    web_records: list[DomainRecord], api_records: list[DomainRecord]
) -> list[DomainRecord]:
    """网页优先且原样保留；API 仅补充网页中不存在的域名。"""
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


def write_outputs(records: list[DomainRecord], output_dir: Path) -> None:
    """生成六个单端口文件，并按 PORTS 顺序逐个拼接成 all.txt。"""
    output_dir.mkdir(parents=True, exist_ok=True)
    rendered: dict[Path, str] = {}

    for port in PORTS:
        lines = [f"{record.host}:{port}#{record.info}" for record in records]
        rendered[output_dir / f"{port}.txt"] = "\n".join(lines) + ("\n" if lines else "")

    # all.txt 是六个端口文件的原样串接：不额外去重、不重排、不改变测速信息。
    all_content = "".join(rendered[output_dir / f"{port}.txt"] for port in PORTS)
    rendered[output_dir / "all.txt"] = all_content

    # 先写完所有临时文件，再逐个替换正式文件，避免写入中断留下半截文件。
    temp_paths: list[tuple[Path, Path]] = []
    try:
        for output_path, content in rendered.items():
            temp_path = output_dir / f".{output_path.name}.vps789.tmp"
            temp_path.write_text(content, encoding="utf-8", newline="\n")
            temp_paths.append((temp_path, output_path))
        for temp_path, output_path in temp_paths:
            temp_path.replace(output_path)
            count = len(output_path.read_text(encoding="utf-8").splitlines())
            print(f"[输出] {output_path.name} -> {count} 行")
    finally:
        for temp_path, _ in temp_paths:
            if temp_path.exists():
                try:
                    temp_path.unlink()
                except OSError:
                    pass


def cleanup_diagnostics(output_dir: Path) -> None:
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
            "抓取 VPS789 全部网页分页，使用 Top20 API 补充，并生成六个端口文件及 all.txt。"
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
    merged = merge_sources(web_records, api_records)
    write_outputs(merged, output_dir)
    cleanup_diagnostics(output_dir)
    print("=== VPS789 优选域名聚合完成 ===")
    print(f"最终输出域名：{len(merged)} 条（无数量上限）")
    print(f"all.txt 汇总记录：{len(merged) * len(PORTS)} 条（六个端口）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
