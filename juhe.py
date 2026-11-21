#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合并 sub、Danfeng、VPS789 三份 host:port 列表，规范化并去重。"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import os
import shutil
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
OUTPUT_FILE = ROOT / "juhe.txt"
LOG_FILE = ROOT / "juhe.log"

# 保留现有读取/输出顺序；去重只删除后续重复行，不代表来源质量优先级。
SOURCES = (
    ("sub/sub.txt", ROOT / "sub" / "sub.txt", True),
    ("danfeng/danfeng.txt", ROOT / "danfeng" / "danfeng.txt", True),
    ("vps789/all.txt", ROOT / "vps789" / "all.txt", True),
)

DOMAIN_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)
PORT_RE = re.compile(r"^[0-9]+$")


def normalize_host(host: str) -> Optional[tuple[str, bool]]:
    """返回 (规范化主机, 是否 IPv6)；不合法则返回 None。"""
    host = host.strip()
    if not host:
        return None

    # 支持 IPv4 / IPv6；ipaddress 会统一压缩 IPv6 表示。
    try:
        address = ipaddress.ip_address(host)
        return address.compressed.lower(), address.version == 6
    except ValueError:
        pass

    # 域名大小写不敏感；尾随点不影响实际主机名。
    host = host.rstrip(".").lower()
    if not host or len(host) > 253 or ":" in host or any(ch in host for ch in "[]/\\@?#"):
        return None

    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None

    labels = host.split(".")
    if any(not label or len(label) > 63 or not DOMAIN_LABEL_RE.fullmatch(label) for label in labels):
        return None

    return host, False


def parse_endpoint(raw_line: str) -> Optional[str]:
    """从一行中提取纯 host:port；自动丢弃 # 后的测速/备注信息。"""
    line = raw_line.strip().lstrip("\ufeff")
    if not line or line.startswith("#"):
        return None

    # VPS789 的格式通常是 host:port#延迟/测速信息。
    line = line.split("#", 1)[0].strip()
    if not line:
        return None

    # 兼容 host:port 后带空格备注的情况。
    line = line.split()[0].strip().strip("\"'")
    if not line:
        return None

    host: str
    port_text: str

    # IPv6 标准格式：[IPv6]:port
    if line.startswith("["):
        closing = line.find("]")
        if closing < 0 or closing + 1 >= len(line) or line[closing + 1] != ":":
            return None
        host = line[1:closing]
        port_text = line[closing + 2 :]
        try:
            address = ipaddress.ip_address(host)
            if address.version != 6:
                return None
            host = address.compressed.lower()
        except ValueError:
            return None
        is_ipv6 = True
    else:
        if ":" not in line:
            return None
        host, port_text = line.rsplit(":", 1)
        normalized = normalize_host(host)
        if normalized is None:
            return None
        host, is_ipv6 = normalized

    if not PORT_RE.fullmatch(port_text):
        return None
    try:
        port_number = int(port_text, 10)
    except ValueError:
        return None
    if not 1 <= port_number <= 65535:
        return None

    # IPv6 必须加方括号，避免与端口分隔符混淆。
    endpoint_host = f"[{host}]" if is_ipv6 else host
    return f"{endpoint_host}:{port_number}"


def read_source_into(
    name: str, path: Path, required: bool, global_unique: dict[str, None]
) -> dict[str, int | bool | str]:
    """Read one input line-by-line and merge into the ordered global key set."""
    stats: dict[str, int | bool | str] = {
        "exists": path.is_file(),
        "lines": 0,
        "valid": 0,
        "invalid": 0,
        "unique": 0,
        "duplicates_inside": 0,
        "new_to_global": 0,
        "error": "",
    }

    if not path.is_file():
        stats["error"] = "文件不存在"
        if required:
            raise FileNotFoundError(f"必需输入文件不存在：{path}")
        return stats

    local_seen: set[str] = set()
    try:
        with path.open("r", encoding="utf-8-sig", errors="strict") as file:
            for raw_line in file:
                if not raw_line.strip():
                    continue
                stats["lines"] = int(stats["lines"]) + 1
                endpoint = parse_endpoint(raw_line)
                if endpoint is None:
                    stats["invalid"] = int(stats["invalid"]) + 1
                    continue
                stats["valid"] = int(stats["valid"]) + 1
                if endpoint in local_seen:
                    continue
                local_seen.add(endpoint)
                if endpoint not in global_unique:
                    global_unique[endpoint] = None
                    stats["new_to_global"] = int(stats["new_to_global"]) + 1
    except OSError as exc:
        stats["error"] = f"读取失败：{exc}"
        if required:
            raise
        return stats

    stats["unique"] = len(local_seen)
    stats["duplicates_inside"] = int(stats["valid"]) - len(local_seen)
    return stats


def read_source(name: str, path: Path, required: bool) -> tuple[list[str], dict[str, int | bool | str]]:
    """Compatibility helper retained for tests and one-off inspection."""
    merged: dict[str, None] = {}
    stats = read_source_into(name, path, required, merged)
    return list(merged), stats


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_outputs_atomically(endpoints, summary: str) -> None:
    """Stream the list to a staged file and restore the previous pair on failure."""
    destinations = (OUTPUT_FILE, LOG_FILE)
    replaced: list[Path] = []

    with tempfile.TemporaryDirectory(prefix=".juhe-stage-", dir=ROOT) as temp_name:
        stage_dir = Path(temp_name)
        staged_output = stage_dir / OUTPUT_FILE.name
        staged_log = stage_dir / LOG_FILE.name
        backups: dict[Path, Path | None] = {}
        for destination in destinations:
            if destination.exists():
                backup = stage_dir / f"backup-{destination.name}"
                shutil.copy2(destination, backup)
                backups[destination] = backup
            else:
                backups[destination] = None

        with staged_output.open("w", encoding="utf-8", newline="\n", buffering=1024 * 1024) as handle:
            if isinstance(endpoints, str):
                for endpoint in endpoints.splitlines():
                    handle.write(endpoint)
                    handle.write("\n")
            else:
                for endpoint in endpoints:
                    handle.write(endpoint)
                    handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        with staged_log.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(summary.rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())

        expected = {
            OUTPUT_FILE: _sha256_file(staged_output),
            LOG_FILE: _sha256_file(staged_log),
        }
        try:
            staged_output.replace(OUTPUT_FILE)
            replaced.append(OUTPUT_FILE)
            staged_log.replace(LOG_FILE)
            replaced.append(LOG_FILE)
            for destination in destinations:
                if _sha256_file(destination) != expected[destination]:
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
                    f"juhe 输出失败：{write_error}；回滚失败：{'；'.join(rollback_errors)}"
                ) from write_error
            raise

def main() -> int:
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S %z")
    global_unique: dict[str, None] = {}
    source_stats: list[tuple[str, dict[str, int | bool | str], int]] = []
    total_valid = 0
    total_lines = 0
    total_invalid = 0

    try:
        for name, path, required in SOURCES:
            stats = read_source_into(name, path, required, global_unique)
            total_lines += int(stats["lines"])
            total_valid += int(stats["valid"])
            total_invalid += int(stats["invalid"])

            new_from_source = int(stats.get("new_to_global", 0))
            # 当前源内重复已单独统计；跨来源重复 = 本源独有数 - 本次新增数。
            source_stats.append((name, stats, new_from_source))

        unique_total = len(global_unique)
        duplicates_removed = total_valid - unique_total
        lines = [
            "╭────────────────────────────────────────────────────────────╮",
            "│ JUHE · 三来源 HOST:PORT 去重汇总报告                       │",
            "╰────────────────────────────────────────────────────────────╯",
            f"运行时间：{now}",
            "处理方式：按 sub → Danfeng → VPS789 顺序读取；统一规范化并去重。",
            "说明：输入顺序只决定重复项的位置，不代表来源质量优先级。",
            "",
            "【一、输入文件明细】",
        ]
        for index, (name, stats, new_count) in enumerate(source_stats, 1):
            lines.extend(
                [
                    f"{index}. {name}",
                    f"   文件状态：{'存在' if stats['exists'] else '缺失'}",
                    f"   读取非空行：{int(stats['lines']):,}",
                    f"   有效 HOST:PORT：{int(stats['valid']):,}",
                    f"   无效/跳过：{int(stats['invalid']):,}",
                    f"   本文件唯一：{int(stats['unique']):,}",
                    f"   文件内部重复：{int(stats['duplicates_inside']):,}",
                    f"   新增到总表：{new_count:,}",
                    f"   与前面来源重复：{int(stats['unique']) - new_count:,}",
                ]
            )
            if stats["error"]:
                lines.append(f"   备注：{stats['error']}")
            lines.append("")

        lines.extend(
            [
                "【二、最终汇总】",
                f"所有来源非空行：{total_lines:,}",
                f"有效 HOST:PORT：{total_valid:,}",
                f"无效/跳过记录：{total_invalid:,}",
                f"最终唯一 HOST:PORT：{unique_total:,}",
                f"移除重复记录：{duplicates_removed:,}",
                f"输出文件：{OUTPUT_FILE.name}",
                "",
                "✓ 汇总完成",
            ]
        )
        write_outputs_atomically(global_unique.keys(), "\n".join(lines))

        print("汇总完成")
        print(f"有效记录：{total_valid}")
        print(f"去重后：{unique_total}")
        print(f"移除重复：{duplicates_removed}")
        print(f"无效/跳过：{total_invalid}")
        print(f"输出：{OUTPUT_FILE.name}；统计：{LOG_FILE.name}")
        return 0

    except Exception as exc:
        error_text = (
            "╭────────────────────────────────────────────────────────────╮\n"
            "│ JUHE · 去重汇总失败                                         │\n"
            "╰────────────────────────────────────────────────────────────╯\n"
            f"运行时间：{now}\n"
            f"错误：{type(exc).__name__}: {exc}\n"
            "未生成新的 juhe.txt；上一份成功结果保持不变。"
        )
        try:
            temp_log = LOG_FILE.with_name(LOG_FILE.name + ".tmp")
            temp_log.write_text(error_text.rstrip() + "\n", encoding="utf-8")
            os.replace(temp_log, LOG_FILE)
        except OSError:
            pass
        print(f"汇总失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
