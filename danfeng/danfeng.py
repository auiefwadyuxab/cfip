#!/usr/bin/env python3
"""Aggregate target IP:port pairs from Danfeng CSV exports.

The target endpoint is read only from the explicit IP address and port columns.
Columns such as 出站IP are intentionally ignored.

CSV files are processed newest first based on YYYYMMDD or YYMMDD tokens,
optionally followed by _HHMMSS. Two-digit years are interpreted as 2000-2099.
The output keeps the
first occurrence of each IP:port, so the newest file wins when duplicates exist.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import ipaddress
import os
import re
import shutil
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

# Match YYYYMMDD or YYMMDD, optionally followed by _HHMMSS. Date tokens
# must not be embedded in a longer digit sequence. YY=00..99 means 2000..2099.
FILENAME_DATE_RE = re.compile(
    r"(?<!\d)(?P<date>20\d{6}|\d{6})"
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
    invalid_rows: int = 0
    invalid_examples: tuple[str, ...] = ()


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


def detect_csv_encoding(path: Path) -> str:
    """Detect a supported encoding with bounded memory before streaming CSV rows."""
    with path.open("rb") as raw:
        prefix = raw.read(4)
    if prefix.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    if prefix.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"

    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    try:
        with path.open("rb") as raw:
            while True:
                chunk = raw.read(1024 * 1024)
                if not chunk:
                    break
                decoder.decode(chunk, final=False)
            decoder.decode(b"", final=True)
        return "utf-8-sig"
    except UnicodeDecodeError:
        # The source files commonly come from Chinese-language export tools.
        # GB18030 covers GBK and remains streamed by TextIOWrapper.
        return "gb18030"


def normalize_ip(value: str, path: Path, line_number: int) -> str:
    """Normalize IPv4/IPv6 values and report malformed rows with their location."""
    raw = (value or "").strip()
    if raw.startswith("[") and raw.endswith("]"):
        raw = raw[1:-1].strip()
    try:
        address = ipaddress.ip_address(raw)
    except ValueError as exc:
        raise CsvDataError(f"{path.name} 第 {line_number} 行 IP 地址无效：{value!r}") from exc
    if address.version == 6:
        return f"[{address.compressed}]"
    return str(address)


def normalize_port(value: str, path: Path, line_number: int) -> str:
    """Normalize a decimal TCP/UDP port and reject values outside 1..65535."""
    raw = (value or "").strip()
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise CsvDataError(f"{path.name} 第 {line_number} 行端口无效：{value!r}")
    port = int(raw)
    if not 1 <= port <= 65535:
        raise CsvDataError(f"{path.name} 第 {line_number} 行端口超出范围：{value!r}")
    return str(port)


def process_csv_file(source: CsvFile, global_entries: dict[str, None]) -> FileStats:
    """Stream a CSV, keep valid IP:port rows, and record malformed rows without aborting."""
    path = source.path
    encoding = detect_csv_encoding(path)
    file_seen: set[str] = set()
    parsed = 0
    newly_added = 0
    invalid_rows = 0
    invalid_examples: list[str] = []
    headers: list[str] | None = None
    ip_index: int | None = None
    port_index: int | None = None

    try:
        with path.open("r", encoding=encoding, errors="strict", newline="") as stream:
            reader = csv.reader(stream, skipinitialspace=True, strict=False)
            for row in reader:
                if not row or all(not cell.strip() for cell in row):
                    continue

                if headers is None:
                    headers = row
                    ip_index = find_column(headers, IP_HEADER_ALIASES, "IP地址", path)
                    port_index = find_column(headers, PORT_HEADER_ALIASES, "端口/端口号", path)
                    continue

                line_number = reader.line_num
                ip_value = row[ip_index] if ip_index is not None and ip_index < len(row) else ""
                port_value = row[port_index] if port_index is not None and port_index < len(row) else ""
                if not (ip_value or "").strip() and not (port_value or "").strip():
                    continue

                try:
                    host = normalize_ip(ip_value, path, line_number)
                    port = normalize_port(port_value, path, line_number)
                except CsvDataError as exc:
                    invalid_rows += 1
                    if len(invalid_examples) < 8:
                        invalid_examples.append(f"第 {line_number} 行：{exc}")
                    continue

                endpoint = f"{host}:{port}"
                parsed += 1
                if endpoint in file_seen:
                    continue
                file_seen.add(endpoint)
                if endpoint not in global_entries:
                    global_entries[endpoint] = None
                    newly_added += 1
    except (OSError, UnicodeError, csv.Error) as exc:
        raise CsvDataError(
            f"{path.name}: 文件读取/CSV 解码失败（{type(exc).__name__}）：{exc}"
        ) from exc

    if headers is None:
        raise CsvDataError(f"{path.name}: 无法识别 CSV 表头。")

    return FileStats(
        source=source,
        parsed=parsed,
        unique_in_file=len(file_seen),
        newly_added=newly_added,
        duplicates=parsed - newly_added,
        invalid_rows=invalid_rows,
        invalid_examples=tuple(invalid_examples),
    )


def extract_from_csv(path: Path) -> list[str]:
    """Compatibility helper used by tests: return valid rows in file order."""
    source = CsvFile(path, *parse_filename_time(path))
    entries: dict[str, None] = {}
    process_csv_file(source, entries)
    return list(entries)

def parse_filename_time(path: Path) -> tuple[datetime | None, bool]:
    """Read YYMMDD and optional _HHMMSS from the filename, not CSV row data."""
    matches = list(FILENAME_DATE_RE.finditer(path.stem))
    for match in reversed(matches):
        token = match.group("date")
        if len(token) == 8:
            year = int(token[:4])
            month = int(token[4:6])
            day = int(token[6:8])
        else:
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
        "╭────────────────────────────────────────────────────────────╮",
        "│ DANFENG · CSV IP 汇总运行报告                               │",
        "╰────────────────────────────────────────────────────────────╯",
        f"生成时间：{generated_at}",
        f"扫描目录：{SOURCE_DIR.name}/（包含子目录）",
        "日期排序：支持 YYYYMMDD、YYMMDD，可带 _HHMMSS；较新的文件优先。",
        "处理规则：有效行继续汇总；无效 IP/端口行跳过并记录示例。",
        "",
        "【一、汇总统计】",
        f"CSV 文件数：{len(csv_files)} 个",
        f"解析记录数：{total} 条",
        f"去重后数量：{unique} 条",
        f"重复记录数：{duplicate_total} 条",
        f"无效行数：{sum(item.invalid_rows for item in stats)} 条（已跳过并记录，不影响有效行）",
        "",
        "【二、文件明细（最新在上）】",
    ]

    for item in stats:
        lines.extend(
            [
                f"[{format_file_time(item.source)}] {item.source.relative_name}",
                f"  解析记录：{item.parsed} 条",
                f"  文件内唯一：{item.unique_in_file} 个 IP:端口",
                f"  新增汇总：{item.newly_added} 个 IP:端口",
                f"  重复记录：{item.duplicates} 条",
                f"  无效行：{item.invalid_rows} 条（已跳过）",
                *(f"    示例：{example}" for example in item.invalid_examples),
                "",
            ]
        )

    if not stats:
        lines.append("（没有 CSV 文件）")

    lines.extend(
        [
            "说明：重复记录 = 全部有效记录数 - 最终唯一 IP:端口数。",
            "说明：按文件名时间从新到旧处理；同一 IP:端口优先保留较新文件的首次记录。",
            "✓ Danfeng 汇总完成",
        ]
    )
    return "\n".join(lines) + "\n"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_outputs_atomically(entries: Iterable[str], log_content: str) -> None:
    """Stream the result to disk and roll back the output/log pair on write failure."""
    outputs = (OUTPUT_FILE, LOG_FILE)
    replaced: list[Path] = []

    with tempfile.TemporaryDirectory(prefix=".danfeng-stage-", dir=SOURCE_DIR) as temp_name:
        stage_dir = Path(temp_name)
        staged_txt = stage_dir / OUTPUT_FILE.name
        staged_log = stage_dir / LOG_FILE.name
        backups: dict[Path, Path | None] = {}

        for destination in outputs:
            if destination.exists():
                backup = stage_dir / f"backup-{destination.name}"
                shutil.copy2(destination, backup)
                backups[destination] = backup
            else:
                backups[destination] = None

        with staged_txt.open("w", encoding="utf-8", newline="\n", buffering=1024 * 1024) as handle:
            for entry in entries:
                handle.write(entry)
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        with staged_log.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(log_content.rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())

        expected = {
            OUTPUT_FILE: _file_sha256(staged_txt),
            LOG_FILE: _file_sha256(staged_log),
        }
        try:
            staged_txt.replace(OUTPUT_FILE)
            replaced.append(OUTPUT_FILE)
            staged_log.replace(LOG_FILE)
            replaced.append(LOG_FILE)
            for destination in outputs:
                if _file_sha256(destination) != expected[destination]:
                    raise OSError(f"{destination.name} 写入后 SHA-256 回读不一致")
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
                raise OSError(
                    f"Danfeng 输出失败：{write_error}；回滚失败：{'；'.join(rollback_errors)}"
                ) from write_error
            raise

def write_failure_log(message: str) -> None:
    """Overwrite the previous report with this run's failure, without touching danfeng.txt."""
    content = "\n".join([
        "╭────────────────────────────────────────────────────────────╮",
        "│ DANFENG · CSV IP 汇总失败                                  │",
        "╰────────────────────────────────────────────────────────────╯",
        f"运行时间：{datetime.now(LOG_TIMEZONE).strftime('%Y-%m-%d %H:%M:%S UTC+08:00')}",
        f"扫描目录：{SOURCE_DIR}",
        f"错误：{message}",
        "旧 danfeng.txt 保持不变。",
        "",
    ])
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", dir=SOURCE_DIR,
            prefix=".danfeng-failure-", suffix=".tmp", delete=False,
        ) as handle:
            temp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, LOG_FILE)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)


def main() -> int:
    csv_files = find_csv_files()
    if not csv_files:
        message = "danfeng 目录中没有找到 CSV；未修改 danfeng.txt。"
        print(f"错误：{message}", file=sys.stderr)
        try:
            write_failure_log(message)
        except OSError as log_error:
            print(f"失败日志写入失败：{log_error}", file=sys.stderr)
        return 1

    # Dict preserves first-seen ordering while avoiding the previous list+set
    # duplication of the complete result in memory.
    all_entries: dict[str, None] = {}
    file_stats: list[FileStats] = []
    total_rows = 0
    total_invalid = 0

    try:
        for source in csv_files:
            stats = process_csv_file(source, all_entries)
            file_stats.append(stats)
            total_rows += stats.parsed
            total_invalid += stats.invalid_rows
            print(
                f"[{source.relative_name}] 有效 {stats.parsed}；文件内唯一 {stats.unique_in_file}；"
                f"新增 {stats.newly_added}；无效行跳过 {stats.invalid_rows}"
            )

        log_content = build_log(csv_files, file_stats, total_rows, len(all_entries))
        write_outputs_atomically(all_entries.keys(), log_content)
    except (CsvDataError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        print("未完成汇总；旧 danfeng.txt 保持不变。", file=sys.stderr)
        try:
            write_failure_log(f"{type(exc).__name__}: {exc}")
        except OSError as log_error:
            print(f"失败日志写入失败：{log_error}", file=sys.stderr)
        return 1

    duplicate_total = total_rows - len(all_entries)
    print(
        f"CSV 文件：{len(csv_files)} 个；解析记录：{total_rows} 条；"
        f"去重后：{len(all_entries)} 条；重复：{duplicate_total} 条；"
        f"无效行跳过：{total_invalid} 条；已生成 danfeng.txt 和 danfeng.log。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
