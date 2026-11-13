#!/usr/bin/env python3
"""Aggregate target IP:port pairs from Danfeng CSV exports.

The target endpoint is read only from the explicit IP address and port columns.
Columns such as 出站IP are intentionally ignored.

CSV files are processed newest first based on YYMMDD[_HHMMSS] tokens in their
filenames (years 25-99 are interpreted as 2025-2099). The output keeps the
first occurrence of each IP:port, so the newest file wins when duplicates exist.
"""

from __future__ import annotations

import csv
import io
import ipaddress
import os
import re
import sys
import tempfile
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable


SOURCE_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = SOURCE_DIR / "danfeng.txt"
LOG_FILE = SOURCE_DIR / "danfeng.log"

# Deliberately exact aliases. 出站IP is not a target endpoint and is excluded.
IP_HEADER_ALIASES = ("ip地址", "ipaddress", "ip")
PORT_HEADER_ALIASES = ("端口号", "端口", "portnumber", "port")

# Match date tokens such as 260609 and 260929_072037. The token must not be
# embedded in a longer digit sequence. YY=25..99 is interpreted as 20YY.
FILENAME_DATE_RE = re.compile(
    r"(?<!\d)(?P<date>(?:2[5-9]|[3-9]\d)\d{4})"
    r"(?:[_-](?P<time>\d{6}))?(?!\d)"
)

# GitHub Actions runners use UTC; logs are easier to read in UTC+08:00.
LOG_TIMEZONE = timezone(timedelta(hours=8), name="UTC+08:00")


class CsvDataError(Exception):
    """Raised when a CSV cannot be parsed safely."""


@dataclass(frozen=True)
class CsvFile:
    path: Path
    file_time: datetime | None
    has_clock_time: bool

    @property
    def relative_name(self) -> str:
        return self.path.relative_to(SOURCE_DIR).as_posix()


@dataclass(frozen=True)
class FileStats:
    source: CsvFile
    parsed: int
    unique_in_file: int
    newly_added: int
    duplicates: int


def normalize_header(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").strip().casefold()
    return re.sub(r"[\s_\-]+", "", value)


def find_column(
    headers: list[str], aliases: Iterable[str], label: str, path: Path
) -> int:
    alias_set = set(aliases)
    matches = [
        index
        for index, header in enumerate(headers)
        if normalize_header(header) in alias_set
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise CsvDataError(
            f"{path.name}: 表头中存在多个「{label}」候选列，无法安全判断。"
        )
    raise CsvDataError(
        f"{path.name}: 找不到「{label}」列。支持的表头："
        f"{', '.join(aliases)}；实际表头：{', '.join(headers)}"
    )


def decode_csv(path: Path) -> str:
    """Decode UTF-8/UTF-8-BOM, UTF-16 BOM, or common Chinese GB18030 CSV."""
    raw = path.read_bytes()

    # Check UTF-16 BOM before GB18030: GB18030 can decode many byte sequences
    # that are actually UTF-16 and would otherwise produce corrupted headers.
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError as exc:
            raise CsvDataError(f"{path.name}: UTF-16 编码损坏。") from exc

    for encoding in ("utf-8-sig", "gb18030", "utf-16"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise CsvDataError(
        f"{path.name}: 文件编码无法识别（已尝试 UTF-8、GB18030、UTF-16）。"
    )


def normalize_ip(value: str, path: Path, line_number: int) -> str:
    raw = (value or "").strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()

    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise CsvDataError(
            f"{path.name}:{line_number}:「IP地址」不是有效的 IPv4/IPv6 地址：{value!r}"
        ) from exc

    if address.version == 6:
        return f"[{address.compressed}]"
    return str(address)


def normalize_port(value: str, path: Path, line_number: int) -> str:
    """Accept every numeric TCP/UDP port, not just a common-port whitelist."""
    raw = (value or "").strip()
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise CsvDataError(
            f"{path.name}:{line_number}:「端口」必须是 1–65535 的整数：{value!r}"
        )

    port = int(raw)
    if not 1 <= port <= 65535:
        raise CsvDataError(
            f"{path.name}:{line_number}:「端口」超出有效范围 1–65535：{value!r}"
        )
    return str(port)


def extract_from_csv(path: Path) -> list[str]:
    """Return every valid IP:port row in its original CSV order."""
    text = decode_csv(path)
    reader = csv.reader(
        io.StringIO(text, newline=""), skipinitialspace=True, strict=True
    )

    headers: list[str] | None = None
    ip_index: int | None = None
    port_index: int | None = None
    entries: list[str] = []

    try:
        for row in reader:
            if not row or all(not cell.strip() for cell in row):
                continue

            if headers is None:
                headers = row
                ip_index = find_column(headers, IP_HEADER_ALIASES, "IP地址", path)
                port_index = find_column(
                    headers, PORT_HEADER_ALIASES, "端口/端口号", path
                )
                continue

            line_number = reader.line_num
            ip_value = row[ip_index] if ip_index is not None and ip_index < len(row) else ""
            port_value = (
                row[port_index]
                if port_index is not None and port_index < len(row)
                else ""
            )

            # Skip a completely empty endpoint row; half-filled rows are errors.
            if not (ip_value or "").strip() and not (port_value or "").strip():
                continue

            host = normalize_ip(ip_value, path, line_number)
            port = normalize_port(port_value, path, line_number)
            entries.append(f"{host}:{port}")
    except csv.Error as exc:
        raise CsvDataError(
            f"{path.name}: CSV 格式错误（约第 {reader.line_num} 行）：{exc}"
        ) from exc

    if headers is None:
        raise CsvDataError(f"{path.name}: 文件为空，没有 CSV 表头。")

    return entries


def parse_filename_time(path: Path) -> tuple[datetime | None, bool]:
    """Read YYMMDD and optional _HHMMSS from the filename, not CSV row data."""
    matches = list(FILENAME_DATE_RE.finditer(path.stem))
    for match in reversed(matches):
        token = match.group("date")
        year = 2000 + int(token[:2])
        month = int(token[2:4])
        day = int(token[4:6])
        clock = match.group("time")

        try:
            parsed_date = datetime(year, month, day)
        except ValueError:
            continue

        if clock:
            try:
                parsed_clock = datetime.strptime(clock, "%H%M%S").time()
                return datetime.combine(parsed_date.date(), parsed_clock), True
            except ValueError:
                # The calendar date is still useful even if the time suffix is bad.
                pass
        return parsed_date, False

    return None, False


def find_csv_files() -> list[CsvFile]:
    files: list[CsvFile] = []
    for path in SOURCE_DIR.rglob("*"):
        if not path.is_file() or path.suffix.casefold() != ".csv":
            continue
        file_time, has_clock_time = parse_filename_time(path)
        files.append(CsvFile(path, file_time, has_clock_time))

    # Stable ordering: dated files first, newest timestamp first; path ascending
    # is used for ties and for files whose names contain no recognized date.
    files.sort(key=lambda item: item.relative_name.casefold())
    files.sort(
        key=lambda item: item.file_time or datetime.min,
        reverse=True,
    )
    return files


def format_file_time(source: CsvFile) -> str:
    if source.file_time is None:
        return "日期未识别（排在末尾）"
    if source.has_clock_time:
        return source.file_time.strftime("%Y-%m-%d %H:%M:%S")
    return source.file_time.strftime("%Y-%m-%d（文件名无时分秒）")


def build_log(csv_files: list[CsvFile], stats: list[FileStats], total: int, unique: int) -> str:
    duplicate_total = total - unique
    generated_at = datetime.now(LOG_TIMEZONE).strftime("%Y-%m-%d %H:%M:%S UTC+08:00")

    lines = [
        "Danfeng CSV IP 汇总日志",
        f"生成时间：{generated_at}",
        "日期来源：CSV 文件名中的 YYMMDD；支持可选的 _HHMMSS；年份 25–99 按 2025–2099 解释",
        f"扫描目录：{SOURCE_DIR.name}/（包含子目录）",
        "",
        "========== 汇总统计 ==========",
        f"CSV 文件数：{len(csv_files)} 个",
        f"解析记录数：{total} 条",
        f"去重后数量：{unique} 条",
        f"重复记录数：{duplicate_total} 条",
        "==============================",
        "",
        "========== 文件明细（最新在上） ==========",
    ]

    for item in stats:
        lines.extend(
            [
                f"[{format_file_time(item.source)}] {item.source.relative_name}",
                f"  解析记录：{item.parsed} 条",
                f"  文件内唯一：{item.unique_in_file} 个 IP:端口",
                f"  新增汇总：{item.newly_added} 个 IP:端口",
                f"  重复记录：{item.duplicates} 条",
                "",
            ]
        )

    if not stats:
        lines.append("（没有 CSV 文件）")

    lines.extend(
        [
            "说明：重复记录 = 全部有效记录数 - 最终唯一 IP:端口数。",
            "说明：按文件名时间从新到旧处理；重复项保留最新文件中首次出现的位置。",
        ]
    )
    return "\n".join(lines) + "\n"


def write_outputs_atomically(txt_content: str, log_content: str) -> None:
    """Stage both outputs before replacing either existing output file."""
    staged: list[tuple[Path, Path]] = []
    try:
        for destination, content in (
            (OUTPUT_FILE, txt_content),
            (LOG_FILE, log_content),
        ):
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                dir=SOURCE_DIR,
                prefix=f".{destination.stem}-",
                suffix=".tmp",
                delete=False,
            ) as temp_file:
                temp_path = Path(temp_file.name)
                temp_file.write(content)
                temp_file.flush()
                os.fsync(temp_file.fileno())
            staged.append((temp_path, destination))

        for temp_path, destination in staged:
            os.replace(temp_path, destination)
    finally:
        for temp_path, _ in staged:
            if temp_path.exists():
                temp_path.unlink()


def main() -> int:
    csv_files = find_csv_files()
    if not csv_files:
        print(
            "错误：danfeng 目录中没有找到 CSV；为避免误清空结果，未修改 danfeng.txt / danfeng.log。",
            file=sys.stderr,
        )
        return 1

    all_entries: list[str] = []
    seen: set[str] = set()
    file_stats: list[FileStats] = []
    total_rows = 0

    try:
        # Files have already been sorted newest-first. Preserve row order within
        # each CSV, and retain the first (therefore newest) occurrence globally.
        for source in csv_files:
            entries = extract_from_csv(source.path)
            file_unique = set(entries)
            before_count = len(seen)

            for entry in entries:
                if entry not in seen:
                    seen.add(entry)
                    all_entries.append(entry)

            newly_added = len(seen) - before_count
            parsed_count = len(entries)
            total_rows += parsed_count
            file_stats.append(
                FileStats(
                    source=source,
                    parsed=parsed_count,
                    unique_in_file=len(file_unique),
                    newly_added=newly_added,
                    duplicates=parsed_count - newly_added,
                )
            )

        txt_content = "".join(f"{entry}\n" for entry in all_entries)
        log_content = build_log(csv_files, file_stats, total_rows, len(all_entries))
        write_outputs_atomically(txt_content, log_content)
    except (CsvDataError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        print(
            "未完成汇总；请先修正 CSV 后重新运行。",
            file=sys.stderr,
        )
        return 1

    duplicate_total = total_rows - len(all_entries)
    print(
        f"CSV 文件：{len(csv_files)} 个；"
        f"解析记录：{total_rows} 条；"
        f"去重后：{len(all_entries)} 条；"
        f"重复：{duplicate_total} 条；"
        "已生成 danfeng.txt 和 danfeng.log。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
