#!/usr/bin/env python3
"""订阅聚合：获取 VLESS 订阅，提取并全局去重 HOST:PORT。"""

from __future__ import annotations

import base64
import binascii
import gzip
import ipaddress
import os
import re
import sys
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = BASE_DIR / "sub.txt"
STATS_FILE = BASE_DIR / "sub_stats.txt"

# 与历史 v2rayNG 抓包保持一致。
USER_AGENT = "v2rayNG/2.2.6"
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Connection": "close",
    "Accept-Encoding": "gzip",
}

MAX_ATTEMPTS = max(1, int(os.getenv("SUB_MAX_ATTEMPTS", "3")))
CONNECT_TIMEOUT = max(1.0, float(os.getenv("SUB_CONNECT_TIMEOUT", "20")))
READ_TIMEOUT = max(1.0, float(os.getenv("SUB_READ_TIMEOUT", "180")))
RETRY_DELAY = max(0.0, float(os.getenv("SUB_RETRY_DELAY", "5")))
RETRY_BACKOFF = max(1.0, float(os.getenv("SUB_RETRY_BACKOFF", "2")))
MAX_REDIRECTS = max(0, int(os.getenv("SUB_MAX_REDIRECTS", "5")))

BLOCKED_ENDPOINTS = {"127.0.0.1:1234"}
BLOCKED_ENDPOINTS_TEXT = "、".join(sorted(BLOCKED_ENDPOINTS))
VLESS_PREFIX = "vless://"
BASE64_RE = re.compile(r"^[A-Za-z0-9+/=_-]+$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
ENDPOINT_RE = re.compile(
    r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+):([1-9][0-9]{0,4})$"
)

# 只统计用户指定的订阅器；其他来源完全不进入统计。
SOURCE_LABELS = (
    "CM",
    "Mia",
    "天诚1",
    "Moist_R",
    "洛璃",
    "辣子鸡",
    "辣椒炒肉少放辣",
    "S5公益",
    "青云志",
    "老王",
    "文烨",
    "Kristi",
    "周润发",
    "DanFeng",
)
SOURCE_LABEL_SET = set(SOURCE_LABELS)
SOURCE_PATTERN = re.compile(r"\[(" + "|".join(re.escape(label) for label in SOURCE_LABELS) + r")\]")


@dataclass
class SubscriptionStats:
    index: int
    vless_lines: int = 0
    valid_endpoints: int = 0
    invalid_lines: int = 0
    blocked_endpoints: int = 0
    duplicate_endpoints: int = 0
    unique_endpoints: int = 0
    contributed_endpoints: int = 0
    sources: dict[str, set[str]] = field(default_factory=dict)
    source_vless_lines: dict[str, int] = field(default_factory=dict)
    source_blocked: dict[str, int] = field(default_factory=dict)

    @property
    def cross_subscription_duplicates(self) -> int:
        return self.unique_endpoints - self.contributed_endpoints

    @property
    def recognized_source_union(self) -> set[str]:
        union: set[str] = set()
        for endpoints in self.sources.values():
            union.update(endpoints)
        return union

    def record_source_line(self, source: str) -> None:
        if source in SOURCE_LABEL_SET:
            self.source_vless_lines[source] = self.source_vless_lines.get(source, 0) + 1

    def record_source_endpoint(self, source: str, endpoint: str) -> None:
        if source in SOURCE_LABEL_SET:
            self.sources.setdefault(source, set()).add(endpoint)

    def record_source_blocked(self, source: str) -> None:
        if source in SOURCE_LABEL_SET:
            self.source_blocked[source] = self.source_blocked.get(source, 0) + 1


def fail(message: str) -> "NoReturn":
    print(f"✖ {message}", file=sys.stderr)
    raise SystemExit(1)


def get_subscription_urls() -> list[str]:
    """读取 Secret SUB；也支持本地命令行参数，一行一个 URL。"""
    raw = os.getenv("SUB", "").strip()
    if not raw and len(sys.argv) > 1:
        raw = "\n".join(arg.strip() for arg in sys.argv[1:] if arg.strip())
    if not raw:
        fail("未检测到订阅地址，请配置 GitHub Secret：sub")

    urls = [line.strip() for line in raw.splitlines() if line.strip()]
    if not urls:
        fail("订阅地址为空")

    for url in urls:
        try:
            parsed = urlsplit(url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                raise ValueError("必须是有效的 HTTP(S) URL")
            # 提前触发非法端口检查。
            _ = parsed.port
        except ValueError as exc:
            fail(f"订阅地址格式错误：{redact(str(exc))}")
    return urls


def safe_url_label(url: str) -> str:
    """日志只显示目标主机，不输出路径、查询参数或 token。"""
    try:
        parsed = urlsplit(url)
        return parsed.hostname or "未知主机"
    except ValueError:
        return "未知主机"


def host_header_for(url: str) -> str | None:
    """生成与实际请求目标一致的 HTTP Host 头；IPv6 保留方括号。"""
    try:
        parsed = urlsplit(url)
        if not parsed.hostname:
            return None
        host = parsed.hostname
        try:
            ip = ipaddress.ip_address(host)
            if ip.version == 6:
                host = f"[{host}]"
        except ValueError:
            pass
        if parsed.port is not None:
            return f"{host}:{parsed.port}"
        return host
    except ValueError:
        return None


def build_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.clear()
    adapter = HTTPAdapter(max_retries=0, pool_connections=1, pool_maxsize=1)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def request_headers_for(url: str) -> dict[str, str]:
    headers = dict(REQUEST_HEADERS)
    host_header = host_header_for(url)
    if host_header:
        headers["Host"] = host_header
    return headers


def fetch(url: str, session: requests.Session) -> str:
    """完整获取单个订阅正文；失败才重试，成功后不重复请求。"""
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        retry_after: str | None = None
        try:
            print(f"  获取 {attempt}/{MAX_ATTEMPTS}：{safe_url_label(url)}")
            current_url = url

            for redirect_index in range(MAX_REDIRECTS + 1):
                headers = request_headers_for(current_url)
                with session.get(
                    current_url,
                    headers=headers,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                    allow_redirects=False,
                    stream=False,
                ) as response:
                    status = response.status_code
                    retry_after = response.headers.get("Retry-After")
                    location = response.headers.get("Location")

                    if status in {301, 302, 303, 307, 308} and location:
                        if redirect_index >= MAX_REDIRECTS:
                            raise ValueError("重定向次数超过上限")
                        next_url = urljoin(current_url, location)
                        parsed_next = urlsplit(next_url)
                        if parsed_next.scheme.lower() not in {"http", "https"} or not parsed_next.hostname:
                            raise ValueError("重定向目标不是有效 HTTP(S) 地址")
                        current_url = next_url
                        continue

                    if status < 200 or status >= 300:
                        raise requests.HTTPError(f"HTTP {status}", response=response)

                    body = response.content  # stream=False：完整读取后才返回
                    break
            else:
                raise ValueError("重定向处理失败")

            if not body:
                raise ValueError("响应正文为空")

            text = decode_subscription_body(body)
            vless_count = count_vless_lines(text)
            if vless_count <= 0:
                raise ValueError("响应已完整读取，但没有发现 VLESS 节点")

            print(f"    ✓ 获取成功：{vless_count} 条 VLESS")
            return text

        except (requests.RequestException, ValueError, UnicodeError, binascii.Error, zlib.error) as exc:
            last_error = exc
            print(
                f"    ! 失败：{type(exc).__name__}：{redact(str(exc))}",
                file=sys.stderr,
            )
            if attempt < MAX_ATTEMPTS:
                delay = retry_delay(attempt, retry_after)
                print(f"    ↻ {delay:g} 秒后再次获取", file=sys.stderr)
                time.sleep(delay)

    assert last_error is not None
    raise last_error


def retry_delay(attempt: int, retry_after: str | None) -> float:
    """优先尊重短 Retry-After；否则使用稳定的递增退避。"""
    if retry_after:
        try:
            value = float(retry_after)
            if 0 <= value <= 60:
                return value
        except ValueError:
            pass
    return RETRY_DELAY * (RETRY_BACKOFF ** (attempt - 1))


def decode_subscription_body(body: bytes) -> str:
    """兼容明文 VLESS、Base64 VLESS，以及少数直接返回 gzip 字节的情况。"""
    payload = body
    if payload[:2] == b"\x1f\x8b":
        payload = gzip.decompress(payload)

    text = payload.decode("utf-8-sig", errors="strict")
    if has_vless(text):
        return text

    compact = re.sub(r"\s+", "", text)
    if len(compact) < 8 or not BASE64_RE.fullmatch(compact):
        raise ValueError("响应既不是明文 VLESS，也不是可识别的 Base64 VLESS")

    normalized = compact.replace("-", "+").replace("_", "/")
    normalized += "=" * ((4 - len(normalized) % 4) % 4)
    decoded = base64.b64decode(normalized, validate=True)
    decoded_text = decoded.decode("utf-8-sig", errors="strict")
    if not has_vless(decoded_text):
        raise ValueError("Base64 解码成功，但内容没有发现 VLESS")
    return decoded_text


def has_vless(text: str) -> bool:
    return count_vless_lines(text) > 0


def count_vless_lines(text: str) -> int:
    return sum(
        1
        for line in text.splitlines()
        if line.lstrip().lower().startswith(VLESS_PREFIX)
    )


def normalize_host(host: str) -> str | None:
    host = host.rstrip(".")
    if not host:
        return None

    try:
        ip = ipaddress.ip_address(host)
        return ip.compressed
    except ValueError:
        pass

    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None

    if len(ascii_host) > 253 or not HOSTNAME_RE.fullmatch(ascii_host):
        return None
    labels = ascii_host.split(".")
    if any(not label or len(label) > 63 or not LABEL_RE.fullmatch(label) for label in labels):
        return None
    return ascii_host.lower()


def extract_host_port(line: str) -> str | None:
    """只提取 VLESS authority 中的 HOST:PORT；过滤由调用方单独完成。"""
    line = line.strip()
    if not line.lower().startswith(VLESS_PREFIX):
        return None

    try:
        parsed = urlsplit(line)
        if parsed.scheme.lower() != "vless" or parsed.hostname is None or parsed.port is None:
            return None
        port = parsed.port
        if not 1 <= port <= 65535:
            return None

        host = normalize_host(parsed.hostname)
        if host is None:
            return None

        endpoint = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        if not ENDPOINT_RE.fullmatch(endpoint):
            return None
        return endpoint
    except ValueError:
        return None


def endpoint_key(endpoint: str) -> str:
    host, port = split_endpoint(endpoint)
    return f"{host.casefold()}:{port}"


def split_endpoint(endpoint: str) -> tuple[str, int]:
    if endpoint.startswith("["):
        close = endpoint.find("]:")
        if close <= 0:
            raise ValueError("无效 IPv6 endpoint")
        return endpoint[1:close], int(endpoint[close + 2 :])
    host, separator, port = endpoint.rpartition(":")
    if not separator or not host:
        raise ValueError("无效 endpoint")
    return host, int(port)


def source_from_line(line: str) -> str | None:
    """从 VLESS 备注中识别用户指定的订阅器标签。"""
    try:
        fragment = unquote(urlsplit(line.strip()).fragment)
    except ValueError:
        return None

    match = SOURCE_PATTERN.search(fragment)
    if match:
        return match.group(1)
    return None


def process_texts(
    texts: Iterable[tuple[str, SubscriptionStats]],
) -> tuple[list[str], int]:
    seen: set[str] = set()
    nodes: list[str] = []
    total_vless = 0

    for text, stats in texts:
        local_seen: set[str] = set()
        for line in text.splitlines():
            if not line.lstrip().lower().startswith(VLESS_PREFIX):
                continue

            stats.vless_lines += 1
            total_vless += 1
            source = source_from_line(line)
            if source is not None:
                stats.record_source_line(source)

            endpoint = extract_host_port(line)
            if endpoint is None:
                stats.invalid_lines += 1
                continue
            if endpoint in BLOCKED_ENDPOINTS:
                stats.blocked_endpoints += 1
                if source is not None:
                    stats.record_source_blocked(source)
                continue

            stats.valid_endpoints += 1
            if source is not None:
                stats.record_source_endpoint(source, endpoint)

            key = endpoint_key(endpoint)
            if key in local_seen:
                stats.duplicate_endpoints += 1
            else:
                local_seen.add(key)
                stats.unique_endpoints += 1

            if key not in seen:
                seen.add(key)
                nodes.append(endpoint)
                stats.contributed_endpoints += 1

    return nodes, total_vless


def validate_nodes(nodes: list[str]) -> None:
    if not nodes:
        fail("没有获得可用的唯一 HOST:PORT，保留现有 sub.txt")

    keys = [endpoint_key(node) for node in nodes]
    if len(set(keys)) != len(nodes):
        fail("内部全局去重校验失败")

    for node in nodes:
        if node in BLOCKED_ENDPOINTS:
            fail(f"过滤校验失败：{node}")
        if not ENDPOINT_RE.fullmatch(node):
            fail(f"输出节点格式错误：{node}")


def write_output(nodes: list[str]) -> None:
    validate_nodes(nodes)
    payload = "\n".join(nodes) + "\n"
    tmp = OUTPUT_FILE.with_name(OUTPUT_FILE.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8", newline="\n")
    tmp.replace(OUTPUT_FILE)

    written_lines = OUTPUT_FILE.read_text(encoding="utf-8").splitlines()
    if written_lines != nodes or len(written_lines) != len(nodes):
        fail("写盘后行数校验失败")
    if any(not line or line != line.strip() or any(ch.isspace() for ch in line) for line in written_lines):
        fail("写盘后发现空行或空白字符")


def _source_status(raw_count: int, unique_count: int, blocked: int) -> tuple[str, str]:
    """返回来源状态和简短说明。"""
    if blocked:
        return "⚠", f"异常：{BLOCKED_ENDPOINTS_TEXT}"
    if unique_count > 0:
        return "✓", "正常"
    if raw_count > 0:
        return "⚠", "异常：无有效 HOST:PORT"
    return "✖", "未返回节点"


def _format_source_diagnostics(stats: SubscriptionStats) -> list[str]:
    """只有实际命中指定订阅器的连接才展开 14 个来源的诊断。"""
    if not stats.source_vless_lines:
        return []

    raw_total = sum(stats.source_vless_lines.values())
    unique_union = stats.recognized_source_union
    normal = 0
    abnormal = 0
    missing = 0
    lines = [
        "  ┌─ 订阅器检测",
        f"  │ 本面板命中 {len(SOURCE_LABELS)} 个指定订阅器标签",
    ]

    for source in SOURCE_LABELS:
        raw_count = stats.source_vless_lines.get(source, 0)
        unique_count = len(stats.sources.get(source, set()))
        blocked = stats.source_blocked.get(source, 0)
        icon, note = _source_status(raw_count, unique_count, blocked)
        if raw_count == 0:
            missing += 1
        elif icon == "✓":
            normal += 1
        else:
            abnormal += 1

        if blocked:
            detail = f"{raw_count} 条 · {unique_count} 个 · {note} · 过滤 {blocked} 条"
        else:
            detail = f"{raw_count} 条 · {unique_count} 个 · {note}"
        lines.append(f"  │ {icon} {source}：{detail}")

    lines.extend(
        [
            "  │",
            f"  │ 合计：{raw_total} 条 VLESS · {len(unique_union)} 个来源唯一 HOST:PORT",
            f"  │ 状态：正常 {normal} · 异常 {abnormal} · 未返回 {missing}",
            "  └─ 以上仅用于订阅器诊断，不参与节点过滤或全局去重",
        ]
    )
    return lines


def write_stats(stats_list: list[SubscriptionStats], total_vless: int, total_nodes: int) -> None:
    """生成简洁运行统计；来源诊断仅展示实际命中的订阅器面板。"""
    lines = [
        "╔══════════════════════════════════════════╗",
        "║             订阅聚合 · 运行统计          ║",
        "╚══════════════════════════════════════════╝",
        f"订阅连接：{len(stats_list)}",
        f"VLESS 总数：{total_vless}",
        f"最终唯一 HOST:PORT：{total_nodes}",
        "",
    ]

    source_connections = 0
    for stats in stats_list:
        has_sources = bool(stats.source_vless_lines)
        if has_sources:
            source_connections += 1
            title = f"【连接 {stats.index:02d}】订阅器面板"
        else:
            title = f"【连接 {stats.index:02d}】节点面板"

        lines.extend(
            [
                title,
                f"  VLESS：{stats.vless_lines} · 有效：{stats.valid_endpoints}",
                f"  本连接唯一：{stats.unique_endpoints} · 本连接重复：{stats.duplicate_endpoints}",
                f"  前序重复：{stats.cross_subscription_duplicates} · 最终贡献：{stats.contributed_endpoints}",
                f"  无效行：{stats.invalid_lines} · 过滤本机：{stats.blocked_endpoints}",
            ]
        )
        if stats.blocked_endpoints:
            lines.append(f"  ⚠ 异常地址：{BLOCKED_ENDPOINTS_TEXT}（共 {stats.blocked_endpoints} 条）")
        source_lines = _format_source_diagnostics(stats)
        if source_lines:
            lines.extend(["", *source_lines])
        lines.append("")

    if source_connections == 0:
        lines.extend(
            [
                "订阅器诊断：本次没有发现指定订阅器标签。",
                "其他来源不进入订阅器诊断。",
            ]
        )
    else:
        lines.insert(6, f"订阅器诊断面板：{source_connections} 个")
        lines.insert(7, "")

    payload = "\n".join(lines).rstrip() + "\n"
    tmp = STATS_FILE.with_name(STATS_FILE.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8", newline="\n")
    tmp.replace(STATS_FILE)

    # 统计文件不得泄露订阅地址或 token。
    written = STATS_FILE.read_text(encoding="utf-8")
    if re.search(r"https?://|(?:^|[^A-Za-z])token[=:]", written, re.IGNORECASE):
        fail("统计文件完整性校验失败：检测到 URL 或 token 字样")



def redact(value: str) -> str:
    # URL 只用于异常信息时彻底隐藏，避免 Secret/token 落入 Action 日志。
    return re.sub(r"https?://[^\s'\"]+", "<已隐藏URL>", value)


def main() -> int:
    print("╔" + "═" * 54 + "╗")
    print("║" + "订阅聚合 · VLESS 节点提取".center(50) + "║")
    print("╚" + "═" * 54 + "╝")
    print("目标：多订阅 → VLESS → HOST:PORT → 全局去重")
    print(f"请求特征：{USER_AGENT} / Connection: close / gzip")
    print(f"重试策略：最多 {MAX_ATTEMPTS} 次，读取超时 {READ_TIMEOUT:g}s")
    print()

    urls = get_subscription_urls()
    print(f"待处理订阅：{len(urls)} 个")
    print()

    session = build_session()
    fetched: list[tuple[str, SubscriptionStats]] = []
    stats_list: list[SubscriptionStats] = []

    try:
        for index, url in enumerate(urls, 1):
            stats = SubscriptionStats(index=index)
            stats_list.append(stats)
            print(f"【连接 {index:02d}/{len(urls):02d}】开始获取")
            try:
                text = fetch(url, session)
            except Exception as exc:
                fail(
                    f"连接 {index:02d} 连续 {MAX_ATTEMPTS} 次未获得有效 VLESS："
                    f"{type(exc).__name__}：{redact(str(exc))}"
                )
            fetched.append((text, stats))
            print()
    finally:
        session.close()

    nodes, total_vless = process_texts(fetched)
    if sum(s.contributed_endpoints for s in stats_list) != len(nodes):
        fail("贡献数量与最终唯一节点数量不一致")

    write_output(nodes)
    write_stats(stats_list, total_vless, len(nodes))

    print("─" * 56)
    for stats in stats_list:
        print(
            f"连接 {stats.index:02d}：VLESS {stats.vless_lines} → 有效 {stats.valid_endpoints} "
            f"→ 唯一 {stats.unique_endpoints} → 前序重复 {stats.cross_subscription_duplicates} "
            f"→ 最终贡献 {stats.contributed_endpoints}"
        )
    print("─" * 56)
    print(f"VLESS 总行数：{total_vless}")
    print(f"最终唯一节点：{len(nodes)}")
    print(f"输出文件：{OUTPUT_FILE}")
    print(f"统计文件：{STATS_FILE}")
    print("✓ 完整性：输出行数 = 全局唯一 HOST:PORT")
    print("✓ 过滤：127.0.0.1:1234")
    source_connection_count = sum(bool(s.source_vless_lines) for s in stats_list)
    if source_connection_count:
        print(f"✓ 订阅器诊断：仅展开 {source_connection_count} 个实际命中指定来源的连接")
    else:
        print("✓ 订阅器诊断：本次未发现指定来源标签")
    print("✓ 其他来源：不进入订阅器诊断")
    print("═" * 56)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
