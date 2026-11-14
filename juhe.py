#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""合并 sub、Danfeng、VPS789 三份 host:port 列表，规范化并去重。"""

from __future__ import annotations

import ipaddress
import re
import sys
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent
OUTPUT_FILE = ROOT / "juhe.txt"
LOG_FILE = ROOT / "juhe.log"

# 按这个顺序读取；重复项保留第一次出现的记录。
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


def read_source(name: str, path: Path, required: bool) -> tuple[list[str], dict[str, int | bool | str]]:
    stats: dict[str, int | bool | str] = {
        "exists": path.is_file(),
        "lines": 0,
        "valid": 0,
        "invalid": 0,
        "unique": 0,
        "duplicates_inside": 0,
        "error": "",
    }

    if not path.is_file():
        stats["error"] = "文件不存在"
        if required:
            raise FileNotFoundError(f"必需输入文件不存在：{path}")
        return [], stats

    records: list[str] = []
    seen: set[str] = set()
    try:
        with path.open("r", encoding="utf-8-sig", errors="replace") as file:
            for raw_line in file:
                if not raw_line.strip():
                    continue
                stats["lines"] = int(stats["lines"]) + 1
                endpoint = parse_endpoint(raw_line)
                if endpoint is None:
                    stats["invalid"] = int(stats["invalid"]) + 1
                    continue
                stats["valid"] = int(stats["valid"]) + 1
                records.append(endpoint)
                seen.add(endpoint)
    except OSError as exc:
        stats["error"] = f"读取失败：{exc}"
        if required:
            raise
        return [], stats

    stats["unique"] = len(seen)
    stats["duplicates_inside"] = int(stats["valid"]) - len(seen)
    return records, stats


def write_log(summary: str) -> None:
    LOG_FILE.write_text(summary.rstrip() + "\n", encoding="utf-8")


def main() -> int:
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S %z")
    global_unique: OrderedDict[str, None] = OrderedDict()
    source_stats: list[tuple[str, dict[str, int | bool | str], int]] = []
    total_valid = 0
    total_lines = 0
    total_invalid = 0

    try:
        for name, path, required in SOURCES:
            records, stats = read_source(name, path, required)
            total_lines += int(stats["lines"])
            total_valid += int(stats["valid"])
            total_invalid += int(stats["invalid"])

            unique_before = len(global_unique)
            for endpoint in records:
                global_unique.setdefault(endpoint, None)
            new_from_source = len(global_unique) - unique_before
            # 当前源内重复已单独统计；跨来源重复 = 本源独有数 - 本次新增数。
            source_stats.append((name, stats, new_from_source))

        output = "".join(endpoint + "\n" for endpoint in global_unique)
        OUTPUT_FILE.write_text(output, encoding="utf-8")

        unique_total = len(global_unique)
        duplicates_removed = total_valid - unique_total
        lines = [
            "IP:端口去重汇总日志",
            f"运行时间：{now}",
            "",
            "输入文件统计：",
        ]
        for name, stats, new_count in source_stats:
            lines.extend(
                [
                    f"- {name}",
                    f"  文件状态：{'存在' if stats['exists'] else '缺失（按空文件处理）'}",
                    f"  非空行数：{stats['lines']}",
                    f"  有效记录：{stats['valid']}",
                    f"  无效/跳过：{stats['invalid']}",
                    f"  本文件去重后：{stats['unique']}",
                    f"  文件内部重复：{stats['duplicates_inside']}",
                    f"  新增到总表：{new_count}",
                    f"  与前面文件重复：{int(stats['unique']) - new_count}",
                ]
            )
            if stats["error"]:
                lines.append(f"  备注：{stats['error']}")

        lines.extend(
            [
                "",
                "总计：",
                f"所有输入非空行：{total_lines}",
                f"有效 host:port 记录：{total_valid}",
                f"无效/跳过记录：{total_invalid}",
                f"去重后总数：{unique_total}",
                f"重复移除总数：{duplicates_removed}",
                f"输出文件：{OUTPUT_FILE.name}",
            ]
        )
        write_log("\n".join(lines))

        print("汇总完成")
        print(f"有效记录：{total_valid}")
        print(f"去重后：{unique_total}")
        print(f"移除重复：{duplicates_removed}")
        print(f"无效/跳过：{total_invalid}")
        print(f"输出：{OUTPUT_FILE.name}；统计：{LOG_FILE.name}")
        return 0

    except Exception as exc:
        error_text = (
            "IP:端口去重汇总失败\n"
            f"运行时间：{now}\n"
            f"错误：{type(exc).__name__}: {exc}\n"
            "未生成新的 juhe.txt。"
        )
        try:
            write_log(error_text)
        except OSError:
            pass
        print(f"汇总失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
