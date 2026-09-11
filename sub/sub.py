#!/usr/bin/env python3
"""订阅聚合：获取 VLESS 订阅并提取唯一 HOST:PORT。"""

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
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = BASE_DIR / "sub.txt"
STATS_FILE = BASE_DIR / "sub_stats.txt"

USER_AGENT = "v2rayNG/2.2.6"
MAX_ATTEMPTS = max(1, int(os.getenv("SUB_MAX_ATTEMPTS", "3")))
CONNECT_TIMEOUT = max(1.0, float(os.getenv("SUB_CONNECT_TIMEOUT", "20")))
READ_TIMEOUT = max(1.0, float(os.getenv("SUB_READ_TIMEOUT", "180")))
RETRY_DELAY = max(0.0, float(os.getenv("SUB_RETRY_DELAY", "4")))
RETRY_BACKOFF = max(1.0, float(os.getenv("SUB_RETRY_BACKOFF", "2")))

# 明确排除这个面板中已知的本机占位节点。
BLOCKED_ENDPOINTS = {"127.0.0.1:1234"}

VLESS_PREFIX = "vless://"
BASE64_RE = re.compile(r"^[A-Za-z0-9+/\s=_-]+$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
ENDPOINT_RE = re.compile(r"^\[[0-9A-Fa-f:.]+\]:([1-9][0-9]{0,4})$|^[A-Za-z0-9.-]+:([1-9][0-9]{0,4})$")

# 用于聚合订阅的来源统计。只根据节点备注中的方括号标签归类，不打印订阅 URL。
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
SOURCE_PATTERN = re.compile(r"\[([^\[\]]+)\]")


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

    def record_source(self, source: str, endpoint: str) -> None:
        self.sources.setdefault(source, set()).add(endpoint)


def fail(message: str) -> "NoReturn":
    print(f"✖ {message}", file=sys.stderr)
    raise SystemExit(1)


def get_subscription_urls() -> list[str]:
    """Read one HTTP(S) URL per line from Secret SUB or CLI arguments."""
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
        except ValueError as exc:
            fail(f"订阅地址格式错误：{exc}")
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            fail("订阅地址必须是有效的 HTTP(S) URL")
    return urls


def safe_url_label(url: str) -> str:
    """Return a log-safe label without query/fragment/token."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or "未知主机"
        return host
    except ValueError:
        return "未知主机"


def expected_host(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if not parsed.hostname:
            return None
        host = parsed.hostname
        if parsed.port is not None and parsed.port not in {80, 443}:
            return f"{host}:{parsed.port}"
        return host
    except ValueError:
        return None


def build_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    session.headers.clear()
    # 与提供的 v2rayNG 抓包保持一致：UA、Connection、Accept-Encoding、Host。
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Connection": "close",
            "Accept-Encoding": "gzip",
        }
    )
    # 应用层自己控制重试，便于严格判断“响应完整且确实包含 VLESS”。
    session.mount("http://", HTTPAdapter(max_retries=0, pool_connections=1, pool_maxsize=1))
    session.mount("https://", HTTPAdapter(max_retries=0, pool_connections=1, pool_maxsize=1))
    return session


def fetch(url: str, session: requests.Session, index: int) -> str:
    """Fetch a complete subscription with bounded retry and content validation."""
    last_error: Exception | None = None
    host_header = expected_host(url)

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            print(f"  获取尝试 {attempt}/{MAX_ATTEMPTS}：{safe_url_label(url)}")
            headers = {"User-Agent": USER_AGENT, "Connection": "close", "Accept-Encoding": "gzip"}
            if host_header:
                headers["Host"] = host_header

            current_url = url
            response = None
            for _redirect in range(6):
                current_host = expected_host(current_url)
                request_headers = dict(headers)
                if current_host:
                    request_headers["Host"] = current_host
                with session.get(
                    current_url,
                    headers=request_headers,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                    allow_redirects=False,
                    stream=False,
                ) as resp:
                    status = resp.status_code
                    retry_after = resp.headers.get("Retry-After")
                    if status in {301, 302, 303, 307, 308} and resp.headers.get("Location"):
                        current_url = requests.compat.urljoin(current_url, resp.headers["Location"])
                        continue
                    body = resp.content  # 完整读取后才进入解析
                    response = resp
                    break
            else:
                raise ValueError("重定向次数过多")

            if status < 200 or status >= 300:
                raise requests.HTTPError(f"HTTP {status}", response=response)
            if not body:
                raise ValueError("响应正文为空")

            text = decode_subscription_body(body)
            count = count_vless_lines(text)
            if count <= 0:
                raise ValueError("响应已完整读取，但未发现 VLESS 节点")

            print(f"    ✓ 获取成功：{count} 条 VLESS")
            return text

        except (requests.RequestException, ValueError, UnicodeError, binascii.Error, zlib.error) as exc:
            last_error = exc
            print(f"    ! 本次失败：{type(exc).__name__}：{redact(str(exc))}", file=sys.stderr)
            if attempt < MAX_ATTEMPTS:
                delay = retry_delay(attempt, retry_after)
                print(f"    ↻ {delay:g} 秒后重试", file=sys.stderr)
                time.sleep(delay)

    assert last_error is not None
    raise last_error


def retry_delay(attempt: int, retry_after: str | None) -> float:
    if retry_after:
        try:
            value = float(retry_after)
            if 0 <= value <= 60:
                return value
        except ValueError:
            pass
    return RETRY_DELAY * (RETRY_BACKOFF ** (attempt - 1))


def decode_subscription_body(body: bytes) -> str:
    """Handle HTTP-level compression when needed, then direct text/Base64 VLESS."""
    # requests 已负责根据 Content-Encoding 解 gzip；这里仍兼容少数代理/上游直接返回 gzip 字节的情况。
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
        raise ValueError("Base64 解码成功，但内容未发现 VLESS")
    return decoded_text


def has_vless(text: str) -> bool:
    return count_vless_lines(text) > 0


def count_vless_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.lstrip().lower().startswith(VLESS_PREFIX))


def normalize_host(host: str) -> str | None:
    host = host.rstrip(".")
    if not host:
        return None
    try:
        ip = ipaddress.ip_address(host)
        return ip.compressed
    except ValueError:
        pass

    # hostname 仅做大小写归一，不做会影响测速目标的额外改写。
    try:
        ascii_host = host.encode("idna").decode("ascii")
    except UnicodeError:
        return None
    if len(ascii_host) > 253 or not HOSTNAME_RE.fullmatch(ascii_host):
        return None
    labels = ascii_host.split('.')
    if any(not label or len(label) > 63 for label in labels):
        return None
    label_re = re.compile(r'^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$')
    if any(not label_re.fullmatch(label) for label in labels):
        return None
    return ascii_host.lower()


def extract_host_port(line: str) -> str | None:
    """Extract only the VLESS authority's HOST:PORT."""
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

        if ":" in host:
            endpoint = f"[{host}]:{port}"
        else:
            endpoint = f"{host}:{port}"

        if not ENDPOINT_RE.fullmatch(endpoint):
            return None
        if endpoint in BLOCKED_ENDPOINTS:
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
    host, sep, port = endpoint.rpartition(":")
    if not sep or not host:
        raise ValueError("无效 endpoint")
    return host, int(port)


def source_from_line(line: str) -> str:
    try:
        fragment = line.split("#", 1)[1] if "#" in line else ""
        from urllib.parse import unquote

        fragment = unquote(fragment)
    except Exception:
        fragment = ""

    bracket_labels = SOURCE_PATTERN.findall(fragment)
    for label in bracket_labels:
        if label in SOURCE_LABELS:
            return label
    return "未识别来源"


def process_texts(texts: Iterable[tuple[str, SubscriptionStats]]) -> tuple[list[str], int]:
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
            endpoint = extract_host_port(line)
            if endpoint is None:
                if "127.0.0.1:1234" in line:
                    stats.blocked_endpoints += 1
                else:
                    stats.invalid_lines += 1
                continue

            stats.valid_endpoints += 1
            source = source_from_line(line)
            stats.record_source(source, endpoint)
            key = endpoint_key(endpoint)

            if key in local_seen:
                stats.duplicate_endpoints += 1
            else:
                local_seen.add(key)
                stats.unique_endpoints += 1

            if key in seen:
                continue
            seen.add(key)
            nodes.append(endpoint)
            stats.contributed_endpoints += 1

    return nodes, total_vless


def validate_nodes(nodes: list[str]) -> None:
    if not nodes:
        fail("没有获得可用的唯一 HOST:PORT，保留现有 sub.txt")

    keys = [endpoint_key(node) for node in nodes]
    if len(set(keys)) != len(nodes):
        fail("内部去重校验失败")

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

    written = OUTPUT_FILE.read_text(encoding="utf-8")
    lines = written.splitlines()
    if lines != nodes or len(lines) != len(nodes):
        fail("写盘后行数校验失败")
    if any(not line or line != line.strip() or any(ch.isspace() for ch in line) for line in lines):
        fail("写盘后发现空行或空白字符")


def write_stats(stats_list: list[SubscriptionStats], total_vless: int, total_nodes: int) -> None:
    lines = [
        "订阅聚合 · 运行统计",
        "=" * 42,
        f"订阅连接数：{len(stats_list)}",
        f"VLESS 总行数：{total_vless}",
        f"最终唯一 HOST:PORT：{total_nodes}",
        "",
    ]
    for stats in stats_list:
        lines.extend(
            [
                f"连接 {stats.index:02d}",
                f"  VLESS：{stats.vless_lines}",
                f"  有效 HOST:PORT：{stats.valid_endpoints}",
                f"  本连接去重后：{stats.unique_endpoints}",
                f"  本连接重复：{stats.duplicate_endpoints}",
                f"  过滤本机：{stats.blocked_endpoints}",
                f"  无效行：{stats.invalid_lines}",
                f"  对最终结果贡献：{stats.contributed_endpoints}",
            ]
        )
        if stats.sources:
            lines.append("  来源唯一节点：")
            for source, endpoints in sorted(stats.sources.items(), key=lambda item: (-len(item[1]), item[0])):
                lines.append(f"    {source}：{len(endpoints)}")
        lines.append("")

    STATS_FILE.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8", newline="\n")


def redact(value: str) -> str:
    return re.sub(r"https?://[^\s'\"]+", "<已隐藏URL>", value)


def main() -> int:
    print("═" * 54)
    print("             订阅聚合 · VLESS 节点提取")
    print("═" * 54)

    urls = get_subscription_urls()
    print(f"订阅连接：{len(urls)} 个")
    print(f"请求环境：{USER_AGENT}")
    print(f"失败重试：最多 {MAX_ATTEMPTS} 次")
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
                text = fetch(url, session, index)
            except Exception as exc:
                fail(f"连接 {index:02d} 连续 {MAX_ATTEMPTS} 次未获得有效 VLESS：{type(exc).__name__}：{redact(str(exc))}")
            fetched.append((text, stats))
            print()
    finally:
        session.close()

    nodes, total_vless = process_texts(fetched)
    write_output(nodes)
    write_stats(stats_list, total_vless, len(nodes))

    print("─" * 54)
    for stats in stats_list:
        print(
            f"连接 {stats.index:02d}：VLESS {stats.vless_lines} → 有效 {stats.valid_endpoints} "
            f"→ 本连接唯一 {stats.unique_endpoints} → 最终贡献 {stats.contributed_endpoints}"
        )
    print("─" * 54)
    print(f"VLESS 总行数：{total_vless}")
    print(f"最终唯一节点：{len(nodes)}")
    print(f"输出文件：{OUTPUT_FILE}")
    print(f"统计文件：{STATS_FILE}")
    print("✓ 行数校验完成：输出行数 = 唯一 HOST:PORT 数")
    print("═" * 54)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
