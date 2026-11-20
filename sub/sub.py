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
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import unquote, urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = BASE_DIR / "sub.txt"
STATS_FILE = BASE_DIR / "sub_stats.txt"

# 与历史 v2rayNG 抓包保持一致。
USER_AGENT = "v2rayNG/2.4.0"
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Connection": "close",
    "Accept-Encoding": "gzip",
}

FETCH_MAX_ATTEMPTS = max(1, int(os.getenv("SUB_FETCH_MAX_ATTEMPTS", os.getenv("SUB_MAX_ATTEMPTS", "3"))))
MAX_ROUNDS = min(5, max(1, int(os.getenv("SUB_MAX_ROUNDS", "5"))))
MIN_SOURCE_ENDPOINTS = 1  # 每个订阅器累计至少一个不同的有效 HOST:PORT 即视为已有正常结果
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

# 订阅器标签以 bestcf.txt 中的 sub://...#标签 为准。
BESTCF_SUBSCRIPTIONS_URL = (
    "https://raw.githubusercontent.com/auiefwadyuxab/cfip/main/bestcf/bestcf.txt"
)
DEFAULT_SOURCE_LABELS = (
    "CM",
    "Mia",
    "天诚1",
    "天诚2",
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
SOURCE_LABELS = DEFAULT_SOURCE_LABELS
SOURCE_LABEL_SET = set(SOURCE_LABELS)
SOURCE_LABEL_REGISTRY_ORIGIN = "内置名单（回退）"
DEFAULT_SOURCE_LABEL_SET = set(DEFAULT_SOURCE_LABELS)
SOURCE_TAG_PATTERN = re.compile(r"\[([^\[\]]{1,96})\]")
SUB_URI_PATTERN = re.compile(r"sub://[^\s\"'<>]+", re.IGNORECASE)


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
    fetch_attempts: int = 0
    failed_attempts: int = 0
    cached_entries_added: int = 0
    discarded_blocked_lines: int = 0
    discarded_invalid_lines: int = 0
    unresolved_sources: list[str] = field(default_factory=list)

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
        if source:
            self.source_vless_lines[source] = self.source_vless_lines.get(source, 0) + 1

    def record_source_endpoint(self, source: str, endpoint: str) -> None:
        if source:
            self.sources.setdefault(source, set()).add(endpoint)

    def record_source_blocked(self, source: str) -> None:
        if source:
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


def extract_source_labels_from_bestcf(content: str) -> tuple[str, ...]:
    """逐行提取 bestcf.txt 中的 sub:// 连接，只读取 # 后标签，忽略其余内容。"""
    labels: list[str] = []
    seen: set[str] = set()
    for raw_line in content.splitlines():
        match = SUB_URI_PATTERN.search(raw_line)
        if not match:
            continue
        # 允许未来每行在链接前后增加说明文字或 Markdown 包装。
        entry = match.group(0).rstrip("),;]}`")
        try:
            parsed = urlsplit(entry)
        except ValueError:
            continue
        if parsed.scheme.lower() != "sub" or not parsed.netloc:
            continue
        label = unquote(parsed.fragment).strip()
        if not label or any(ord(char) < 32 or ord(char) == 127 for char in label):
            continue
        if label not in seen:
            labels.append(label)
            seen.add(label)
    return tuple(labels)


def load_source_labels(session: requests.Session) -> tuple[str, ...]:
    """优先从仓库 bestcf.txt 获取标签；网络读取失败时回退到已知 15 个标签。"""
    global SOURCE_LABEL_REGISTRY_ORIGIN
    try:
        with session.get(
            BESTCF_SUBSCRIPTIONS_URL,
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"},
            timeout=(10, 20),
            allow_redirects=True,
        ) as response:
            response.raise_for_status()
            content = response.content.decode("utf-8-sig", errors="strict")
        labels = extract_source_labels_from_bestcf(content)
        if not labels:
            raise ValueError("bestcf.txt 中没有找到有效的 sub://标签")
        SOURCE_LABEL_REGISTRY_ORIGIN = "bestcf.txt"
        print(f"订阅器名单：从 bestcf.txt 提取 {len(labels)} 个 sub:// 标签")
        return labels
    except (requests.RequestException, UnicodeError, ValueError, OSError) as exc:
        SOURCE_LABEL_REGISTRY_ORIGIN = "内置名单（回退）"
        print(
            "⚠ 无法读取 bestcf.txt，回退到内置的 "
            f"{len(DEFAULT_SOURCE_LABELS)} 个订阅器标签："
            f"{type(exc).__name__}：{redact(str(exc))}",
            file=sys.stderr,
        )
        return DEFAULT_SOURCE_LABELS


def set_source_labels(labels: tuple[str, ...]) -> None:
    """设置本轮运行唯一可信的订阅器标签集合。"""
    global SOURCE_LABELS, SOURCE_LABEL_SET
    SOURCE_LABELS = tuple(labels)
    SOURCE_LABEL_SET = set(labels)


def request_headers_for(url: str) -> dict[str, str]:
    headers = dict(REQUEST_HEADERS)
    host_header = host_header_for(url)
    if host_header:
        headers["Host"] = host_header
    return headers


def fetch(
    url: str,
    session: requests.Session,
    max_attempts: int | None = None,
) -> str:
    """完整获取单个订阅正文；默认使用全局重试次数，也可指定单次请求。"""
    attempt_limit = FETCH_MAX_ATTEMPTS if max_attempts is None else max(1, int(max_attempts))
    last_error: Exception | None = None

    for attempt in range(1, attempt_limit + 1):
        retry_after: str | None = None
        try:
            print(f"  获取 {attempt}/{attempt_limit}：{safe_url_label(url)}")
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
                        retry_after = response.headers.get("Retry-After")
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
            if attempt < attempt_limit:
                delay = retry_delay(attempt, retry_after)
                print(f"    ↻ {delay:g} 秒后再次获取", file=sys.stderr)
                time.sleep(delay)

    assert last_error is not None
    raise last_error


def retry_delay(attempt: int, retry_after: str | None) -> float:
    """支持 Retry-After 秒数和 HTTP 日期；有效服务端值不再限制为 60 秒。"""
    if retry_after:
        value = retry_after.strip()
        try:
            seconds = float(value)
            if seconds >= 0 and seconds != float("inf"):
                return seconds
        except (ValueError, OverflowError):
            pass

        try:
            retry_at = parsedate_to_datetime(value)
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            seconds = (retry_at.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
            if seconds >= 0:
                return seconds
            # 时间点已过，立即允许下一次重试。
            return 0.0
        except (TypeError, ValueError, OverflowError):
            pass

    return RETRY_DELAY * (RETRY_BACKOFF ** (attempt - 1))


def decode_subscription_body(body: bytes) -> str:
    """兼容明文 VLESS、Base64 VLESS，以及少数直接返回 gzip 字节的情况。"""
    payload = body
    if payload[:2] == b"\x1f\x8b":
        try:
            payload = gzip.decompress(payload)
        except (EOFError, OSError, zlib.error) as exc:
            raise ValueError("gzip 响应解压失败或数据不完整") from exc

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
    """仅匹配 bestcf.txt 的 sub:// 连接 # 后标签，不再猜测未知重复标签。"""
    try:
        fragment = unquote(urlsplit(line.strip()).fragment)
    except ValueError:
        return None

    for match in SOURCE_TAG_PATTERN.finditer(fragment):
        label = match.group(1).strip()
        if label in SOURCE_LABEL_SET:
            return label
    return None


def cache_valid_response(
    text: str,
    source_cache: dict[str, dict[str, str]],
    unlabelled_cache: dict[str, str],
    source_metrics: dict[str, dict[str, int]],
    unlabelled_metrics: dict[str, int],
) -> tuple[int, int, int, dict[str | None, dict[str, object]]]:
    """把本轮有效 HOST:PORT 累积到本次运行内存缓存，并保留异常诊断。

    缓存按订阅器标签 + HOST:PORT 去重。127.0.0.1:1234 和无效地址不入缓存；
    后续异常响应不会清除先前正常结果。round_metrics 只统计当前这一轮，
    source_metrics / unlabelled_metrics 则累计整个 sub.py 进程期间的数据。
    """
    added = 0
    blocked = 0
    invalid = 0
    round_metrics: dict[str | None, dict[str, object]] = {}

    for line in text.splitlines():
        if not line.lstrip().lower().startswith(VLESS_PREFIX):
            continue

        source = source_from_line(line)
        round_item = round_metrics.setdefault(
            source,
            {"raw": 0, "blocked": 0, "invalid": 0, "endpoints": set()},
        )
        round_item["raw"] += 1

        if source:
            cumulative = source_metrics.setdefault(
                source, {"raw": 0, "blocked": 0, "invalid": 0}
            )
            cumulative["raw"] += 1
        else:
            unlabelled_metrics["raw"] += 1

        endpoint = extract_host_port(line)
        if endpoint is None:
            invalid += 1
            round_item["invalid"] += 1
            if source:
                cumulative["invalid"] += 1
            else:
                unlabelled_metrics["invalid"] += 1
            continue

        if endpoint in BLOCKED_ENDPOINTS:
            blocked += 1
            round_item["blocked"] += 1
            if source:
                cumulative["blocked"] += 1
            else:
                unlabelled_metrics["blocked"] += 1
            continue

        key = endpoint_key(endpoint)
        round_item["endpoints"].add(key)
        bucket = source_cache.setdefault(source, {}) if source else unlabelled_cache
        if key not in bucket:
            bucket[key] = line.strip()
            added += 1

    return added, blocked, invalid, round_metrics


def summarize_sources(
    panel_caches: list[dict[str, dict]],
) -> dict[str, dict[str, object]]:
    """跨本次运行的所有面板汇总各订阅器的缓存节点和异常计数。"""
    summary: dict[str, dict[str, object]] = {}

    for panel in panel_caches:
        for source, endpoints in panel["sources"].items():
            item = summary.setdefault(
                source,
                {
                    "endpoints": {},
                    "raw": 0,
                    "blocked": 0,
                    "invalid": 0,
                },
            )
            merged_endpoints = item["endpoints"]
            for key, line in endpoints.items():
                merged_endpoints.setdefault(key, line)

        for source, metrics in panel["source_metrics"].items():
            item = summary.setdefault(
                source,
                {
                    "endpoints": {},
                    "raw": 0,
                    "blocked": 0,
                    "invalid": 0,
                },
            )
            item["raw"] += metrics["raw"]
            item["blocked"] += metrics["blocked"]
            item["invalid"] += metrics["invalid"]

    return summary


def recovered_sources(
    panel_caches: list[dict[str, dict]],
) -> set[str]:
    """每个订阅器累计至少有一个不同的有效 HOST:PORT 即视为已有正常结果。"""
    summary = summarize_sources(panel_caches)
    return {
        source
        for source, item in summary.items()
        if len(item["endpoints"]) >= MIN_SOURCE_ENDPOINTS
    }


def build_cached_panel_text(
    source_cache: dict[str, dict[str, str]],
    unlabelled_cache: dict[str, str],
) -> str:
    """生成最终面板文本；每个来源缓存内去重，所有有效节点都参与最终输出。"""
    lines: list[str] = []
    emitted: set[str] = set()

    # 每个已识别订阅器先在自己的缓存桶内按 HOST:PORT 去重。
    for source in SOURCE_LABELS:
        for line in source_cache.get(source, {}).values():
            if line not in emitted:
                lines.append(line)
                emitted.add(line)

    # 兼容标签注册表之外或没有标签的有效 VLESS 节点；同样保留所有有效缓存。
    for source, bucket in source_cache.items():
        if source in SOURCE_LABEL_SET:
            continue
        for line in bucket.values():
            if line not in emitted:
                lines.append(line)
                emitted.add(line)

    for line in unlabelled_cache.values():
        if line not in emitted:
            lines.append(line)
            emitted.add(line)

    return "\n".join(lines)


def describe_round_source(
    raw_count: int,
    endpoint_count: int,
    blocked_count: int,
    invalid_count: int,
    cached_count: int,
) -> tuple[str, str]:
    """返回本轮状态和累计缓存状态；一个有效节点即可算有正常结果。"""
    if raw_count == 0:
        round_status = "本轮未返回（0 条）"
    elif blocked_count and endpoint_count:
        round_status = (
            f"本轮有有效节点，同时发现异常地址 {BLOCKED_ENDPOINTS_TEXT}，"
            f"过滤 {blocked_count} 条"
        )
    elif blocked_count:
        round_status = f"本轮异常：{BLOCKED_ENDPOINTS_TEXT}，过滤 {blocked_count} 条"
    elif endpoint_count:
        round_status = "本轮正常"
    else:
        round_status = "异常：没有有效 HOST:PORT"

    if invalid_count:
        round_status += f"；无效 HOST:PORT {invalid_count} 条"

    cache_status = (
        f"累计缓存 {cached_count} 个有效节点（已有正常结果）"
        if cached_count >= MIN_SOURCE_ENDPOINTS
        else "累计尚无有效节点缓存"
    )
    return round_status, cache_status

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
    """一个有效节点即可视为正常；过滤异常地址仍会单独计入日志。"""
    if unique_count >= MIN_SOURCE_ENDPOINTS:
        return "✓", "正常（至少有一个有效节点）"
    if blocked:
        return "⚠", f"异常：{BLOCKED_ENDPOINTS_TEXT}"
    if raw_count > 0:
        return "⚠", "异常：没有有效 HOST:PORT"
    return "✖", "未返回节点"


def _format_source_diagnostics(stats: SubscriptionStats) -> list[str]:
    """按当前 sub:// 订阅名单展示来源；名单新增项单独标记，不猜测节点标签。"""
    if not stats.source_vless_lines:
        return []

    raw_total = sum(stats.source_vless_lines.values())
    unique_union = stats.recognized_source_union
    normal = 0
    abnormal = 0
    missing = 0
    known_labels = [label for label in SOURCE_LABELS if label in DEFAULT_SOURCE_LABEL_SET]
    added_labels = [label for label in SOURCE_LABELS if label not in DEFAULT_SOURCE_LABEL_SET]
    lines = [
        "  ┌─ 订阅器检测",
        f"  │ 本轮标签：{len(SOURCE_LABELS)} 个 sub://（来源：{SOURCE_LABEL_REGISTRY_ORIGIN}）",
    ]

    def append_label(source: str, prefix: str) -> None:
        nonlocal normal, abnormal, missing
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
        lines.append(f"  │ {prefix}{source}：{detail}")
        if blocked:
            lines.append(
                f"  │   ⚠ 曾发现异常地址：{BLOCKED_ENDPOINTS_TEXT}（过滤 {blocked} 条）"
            )

    for source in known_labels:
        icon, _ = _source_status(
            stats.source_vless_lines.get(source, 0),
            len(stats.sources.get(source, set())),
            stats.source_blocked.get(source, 0),
        )
        append_label(source, f"{icon} ")

    if added_labels:
        lines.append("  │")
        lines.append("  │ 新发现订阅器（bestcf.txt 新增 sub:// 标签）")
        for source in added_labels:
            append_label(source, "+ ")

    lines.extend(
        [
            "  │",
            f"  │ 合计：{raw_total} 条 VLESS · {len(unique_union)} 个来源唯一 HOST:PORT",
            f"  │ 状态：正常 {normal} · 异常 {abnormal} · 未返回 {missing}",
            "  └─ 仅匹配订阅名单中的 sub:// 标签，不参与节点过滤或全局去重",
        ]
    )
    return lines


def write_stats(
    stats_list: list[SubscriptionStats],
    total_vless: int,
    total_nodes: int,
    source_summary: dict[str, dict[str, object]],
    completed_rounds: int,
    panel_caches: list[dict[str, object]],
    round_history: list[dict[str, object]],
) -> None:
    """生成本次运行报告，逐轮保留各订阅器结果与异常历史。"""
    lines = [
        "╔══════════════════════════════════════════╗",
        "║             订阅聚合 · 运行统计          ║",
        "╚══════════════════════════════════════════╝",
        f"订阅连接：{len(stats_list)}",
        f"实际抓取轮次：{completed_rounds}/{MAX_ROUNDS}",
        f"最终缓存 VLESS 行数：{total_vless}",
        f"最终全局唯一 HOST:PORT：{total_nodes}",
        "",
        "逐轮订阅器检测（标签来自 bestcf.txt 中 sub:// 链接 # 后缀）：",
    ]

    for round_item in round_history:
        round_number = int(round_item["number"])
        lines.append(f"【第 {round_number}/{MAX_ROUNDS} 轮】")
        source_metrics = round_item["sources"]
        for source in SOURCE_LABELS:
            metric = source_metrics[source]
            raw_count = int(metric["raw"])
            round_unique = int(metric["unique"])
            blocked_count = int(metric["blocked"])
            invalid_count = int(metric["invalid"])
            cached_count = int(metric["cached"])
            round_status = str(metric["round_status"])
            cache_status = str(metric["cache_status"])

            detail = (
                f"  {source}：{raw_count} 条 · 本轮有效唯一 {round_unique} 个 · "
                f"{round_status} · {cache_status}"
            )
            lines.append(detail)
            if blocked_count:
                lines.append(
                    f"    ⚠ 异常地址：{BLOCKED_ENDPOINTS_TEXT}（本轮过滤 {blocked_count} 条）"
                )
            if invalid_count:
                lines.append(f"    ⚠ 无效 HOST:PORT：{invalid_count} 条（未缓存）")

        panel_errors = round_item.get("panel_errors", [])
        if panel_errors:
            lines.append("  请求失败的连接：")
            lines.extend(f"    {error}" for error in panel_errors)
        lines.append(
            f"  本轮新增面板缓存条目：{round_item['added']} 个；"
            f"仍无有效结果的订阅器：{len(round_item['pending_sources'])} 个"
        )
        lines.append("")

    lines.extend(
        [
            "订阅器累计内存缓存汇总：",
            f"正常结果判定：每个订阅器累计至少 {MIN_SOURCE_ENDPOINTS} 个不同、有效且非本机的 HOST:PORT，即视为已有正常结果。",
            f"缓存范围：仅本次 sub.py 运行期间；最多 {MAX_ROUNDS} 轮；不写缓存文件、不跨 GitHub Actions 运行复用。",
            "合并规则：每轮所有连接都会重新请求；有效结果按订阅器 + HOST:PORT 分桶缓存，后续异常/空结果不覆盖此前缓存；最终所有有效缓存参与合并。",
            "",
        ]
    )

    normal_sources = 0
    empty_sources = 0
    for source in SOURCE_LABELS:
        item = source_summary.get(
            source,
            {"endpoints": {}, "raw": 0, "blocked": 0, "invalid": 0},
        )
        endpoint_count = len(item["endpoints"])
        raw_count = int(item["raw"])
        blocked_count = int(item["blocked"])
        invalid_count = int(item["invalid"])

        if endpoint_count >= MIN_SOURCE_ENDPOINTS:
            status = f"正常（已有 {endpoint_count} 个有效节点）"
            normal_sources += 1
        else:
            status = "五轮内未取得有效节点（可能为 0 条、异常地址或无效地址）"
            empty_sources += 1

        detail = f"  {source}：累计唯一 {endpoint_count} 个 · {status} · 累计原始 {raw_count} 条"
        if blocked_count:
            detail += f" · 曾异常：{BLOCKED_ENDPOINTS_TEXT} · 累计过滤 {blocked_count} 条"
        if invalid_count:
            detail += f" · 累计无效 HOST:PORT {invalid_count} 条"
        lines.append(detail)

    lines.append(
        f"订阅器缓存状态：已有正常结果 {normal_sources} · 仍无有效结果 {empty_sources}"
    )
    lines.append("说明：一个有效 HOST:PORT 即算已有正常结果；无有效结果的来源继续重试，最多 5 轮。")
    lines.append("")

    source_connections = 0
    for stats, panel in zip(stats_list, panel_caches):
        has_sources = bool(panel["source_metrics"])
        title = f"【连接 {stats.index:02d}】订阅器面板" if has_sources else f"【连接 {stats.index:02d}】节点面板"
        lines.extend(
            [
                title,
                f"  抓取轮次：{stats.fetch_attempts}/{MAX_ROUNDS} · 请求失败：{stats.failed_attempts} 次",
                f"  新增内存缓存：{stats.cached_entries_added} 个",
                f"  过滤异常地址：{stats.discarded_blocked_lines} 条 · 无效 HOST:PORT：{stats.discarded_invalid_lines} 条",
            ]
        )
        if has_sources:
            source_connections += 1
            source_lines = _format_source_diagnostics(stats)
            if source_lines:
                lines.extend(["", *source_lines])
        unlabelled = panel["unlabelled"]
        unlabelled_metrics = panel["unlabelled_metrics"]
        if unlabelled_metrics["raw"] or (not has_sources and stats.failed_attempts):
            status = "正常（已有有效节点）" if unlabelled else "未取得有效节点"
            detail = f"  未标注来源：累计原始 {unlabelled_metrics['raw']} 条 · 缓存 {len(unlabelled)} 个 · {status}"
            if unlabelled_metrics["blocked"]:
                detail += f" · 曾异常：{BLOCKED_ENDPOINTS_TEXT} · 过滤 {unlabelled_metrics['blocked']} 条"
            if unlabelled_metrics["invalid"]:
                detail += f" · 无效 {unlabelled_metrics['invalid']} 条"
            lines.append(detail)
        lines.append("")

    if source_connections:
        lines.insert(7, f"订阅器诊断面板：{source_connections} 个")
        lines.insert(8, "")
    else:
        lines.append("订阅器诊断：最终缓存中没有匹配到 bestcf.txt 的 sub:// 标签。")

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
    print(f"内存缓存重试：最多 {MAX_ROUNDS} 轮（含首轮），每个输入连接每轮请求一次")
    print("订阅器成功标准：累计至少有一个不同、有效且非本机的 HOST:PORT 即算已有正常结果。")
    print("缓存规则：所有轮次正常结果持续累积；0 条、127.0.0.1:1234 和无效地址不入缓存；异常历史逐轮记录。")
    print("订阅器分类：读取 bestcf.txt 中每个 sub:// 链接 # 后面的标签，并匹配 VLESS 节点标签。")
    print()

    urls = get_subscription_urls()
    print(f"待处理输入连接：{len(urls)} 个")
    print()

    session = build_session()
    stats_list = [SubscriptionStats(index=index) for index in range(1, len(urls) + 1)]
    panel_caches: list[dict[str, object]] = []
    pending_sources: list[str] = []
    pending_panels: list[str] = []
    completed_rounds = 0
    round_history: list[dict[str, object]] = []

    try:
        set_source_labels(load_source_labels(session))
        panel_caches = [
            {
                "sources": {source: {} for source in SOURCE_LABELS},
                "unlabelled": {},
                "source_metrics": {},
                "unlabelled_metrics": {"raw": 0, "blocked": 0, "invalid": 0},
            }
            for _ in urls
        ]

        for round_number in range(1, MAX_ROUNDS + 1):
            completed_rounds = round_number
            print(f"╭─ 第 {round_number}/{MAX_ROUNDS} 轮：重新请求全部 {len(urls)} 个输入连接")
            round_added = 0
            round_retry_delays: list[float] = []
            round_panel_errors: list[str] = []
            round_source_metrics: dict[str | None, dict[str, object]] = {
                source: {"raw": 0, "blocked": 0, "invalid": 0, "endpoints": set()}
                for source in SOURCE_LABELS
            }

            # 每轮都请求所有输入连接。每个响应里的有效节点独立缓存；
            # 同一订阅器内部按 HOST:PORT 去重，不同订阅器之间先分别保留。
            for index, (url, stats, panel_cache) in enumerate(
                zip(urls, stats_list, panel_caches), 1
            ):
                stats.fetch_attempts += 1
                print(f"【连接 {index:02d}/{len(urls):02d}】第 {round_number}/{MAX_ROUNDS} 轮")
                try:
                    # 一轮对每个输入连接只发起一次请求，五轮即最多五次（不含 HTTP 重定向）。
                    text = fetch(url, session, max_attempts=1)
                except Exception as exc:
                    stats.failed_attempts += 1
                    response = getattr(exc, "response", None)
                    retry_after = response.headers.get("Retry-After") if response is not None else None
                    if retry_after:
                        round_retry_delays.append(retry_delay(round_number, retry_after))
                    error_text = f"连接 {index:02d}：{type(exc).__name__}：{redact(str(exc))}"
                    round_panel_errors.append(error_text)
                    print(
                        f"  ⚠ 本轮请求未取得有效 VLESS；此前内存缓存保留："
                        f"{type(exc).__name__}：{redact(str(exc))}",
                        file=sys.stderr,
                    )
                    continue

                added, blocked, invalid, response_metrics = cache_valid_response(
                    text,
                    panel_cache["sources"],
                    panel_cache["unlabelled"],
                    panel_cache["source_metrics"],
                    panel_cache["unlabelled_metrics"],
                )
                stats.cached_entries_added += added
                stats.discarded_blocked_lines += blocked
                stats.discarded_invalid_lines += invalid
                round_added += added

                for source, metric in response_metrics.items():
                    combined = round_source_metrics.setdefault(
                        source,
                        {"raw": 0, "blocked": 0, "invalid": 0, "endpoints": set()},
                    )
                    combined["raw"] += int(metric["raw"])
                    combined["blocked"] += int(metric["blocked"])
                    combined["invalid"] += int(metric["invalid"])
                    combined["endpoints"].update(metric["endpoints"])

                print(
                    f"  ✓ 本连接新增缓存 {added} 个；过滤本机地址 {blocked} 条；"
                    f"无效 HOST:PORT {invalid} 条"
                )

            # 累计所有面板、所有轮次的各订阅器缓存；不让本轮异常覆盖旧缓存。
            current_summary = summarize_sources(panel_caches)
            round_source_report: dict[str, dict[str, object]] = {}
            for source in SOURCE_LABELS:
                metric = round_source_metrics[source]
                raw_count = int(metric["raw"])
                round_unique = len(metric["endpoints"])
                blocked_count = int(metric["blocked"])
                invalid_count = int(metric["invalid"])
                cached_count = len(current_summary.get(source, {}).get("endpoints", {}))
                round_status, cache_status = describe_round_source(
                    raw_count,
                    round_unique,
                    blocked_count,
                    invalid_count,
                    cached_count,
                )
                is_round_normal = (
                    round_unique >= MIN_SOURCE_ENDPOINTS
                    and blocked_count == 0
                )
                icon = "✓" if is_round_normal else "⚠"
                print(
                    f"{icon} 第 {round_number}/{MAX_ROUNDS} 轮 {source}："
                    f"{raw_count} 条 · 本轮有效唯一 {round_unique} 个 · "
                    f"{round_status} · {cache_status}"
                )
                if blocked_count:
                    print(
                        f"    ⚠ 异常地址：{BLOCKED_ENDPOINTS_TEXT}（本轮过滤 {blocked_count} 条；"
                        f"累计过滤见最终统计）"
                    )
                round_source_report[source] = {
                    "raw": raw_count,
                    "unique": round_unique,
                    "blocked": blocked_count,
                    "invalid": invalid_count,
                    "cached": cached_count,
                    "round_status": round_status,
                    "cache_status": cache_status,
                }

            recovered = recovered_sources(panel_caches)
            pending_sources = [source for source in SOURCE_LABELS if source not in recovered]

            # 未标注的节点面板仍沿用原有处理：也参与本轮缓存和补抓判断。
            pending_panels = []
            for index, (panel_cache, stats) in enumerate(zip(panel_caches, stats_list), 1):
                has_labelled_source = bool(panel_cache["source_metrics"])
                unlabelled_count = len(panel_cache["unlabelled"])
                unlabelled_raw = panel_cache["unlabelled_metrics"]["raw"]
                if has_labelled_source and unlabelled_raw == 0:
                    continue
                if unlabelled_count >= MIN_SOURCE_ENDPOINTS:
                    continue
                if unlabelled_raw > 0 or (not has_labelled_source and stats.failed_attempts > 0):
                    pending_panels.append(f"连接 {index:02d}")

            round_history.append(
                {
                    "number": round_number,
                    "sources": round_source_report,
                    "added": round_added,
                    "pending_sources": list(pending_sources),
                    "pending_panels": list(pending_panels),
                    "panel_errors": list(round_panel_errors),
                }
            )

            print(
                f"本轮汇总：已有正常缓存 {len(recovered)}/{len(SOURCE_LABELS)} 个订阅器，"
                f"本轮新增面板缓存 {round_added} 个。"
            )

            if not pending_sources and not pending_panels:
                print(
                    "✓ 所有订阅器均已至少缓存一个不同的有效 HOST:PORT，"
                    "提前结束重试。"
                )
                break

            if round_number < MAX_ROUNDS:
                if pending_sources:
                    print("仍未获得有效结果的订阅器：" + "、".join(pending_sources))
                if pending_panels:
                    print("仍未获得有效结果的未标注面板：" + "、".join(pending_panels))
                delay = max(round_retry_delays, default=retry_delay(round_number, None))
                print(f"↻ {delay:g} 秒后进入下一轮；所有已缓存正常结果继续保留并累积。")
                if delay > 0:
                    time.sleep(delay)
            else:
                print(
                    f"⚠ 已达到最多 {MAX_ROUNDS} 轮；现在合并所有已缓存的有效结果，"
                    "没有有效结果的来源不会影响其他正常缓存。"
                )

    finally:
        session.close()

    source_summary = summarize_sources(panel_caches)
    # 每个面板先对每个订阅器独立去重；之后 process_texts 再进行全局 HOST:PORT 去重。
    fetched: list[tuple[str, SubscriptionStats]] = []
    for stats, panel_cache in zip(stats_list, panel_caches):
        text = build_cached_panel_text(
            panel_cache["sources"],
            panel_cache["unlabelled"],
        )
        fetched.append((text, stats))
        stats.unresolved_sources = list(pending_sources)

    nodes, total_vless = process_texts(fetched)
    if sum(stats.contributed_endpoints for stats in stats_list) != len(nodes):
        fail("贡献数量与最终全局唯一节点数量不一致")
    if not nodes:
        fail(
            f"最多 {completed_rounds} 轮后仍未获得任何有效 HOST:PORT；"
            "保留现有 sub.txt，不写入空结果。"
        )

    write_output(nodes)
    write_stats(
        stats_list,
        total_vless,
        len(nodes),
        source_summary,
        completed_rounds,
        panel_caches,
        round_history,
    )

    print("─" * 56)
    for stats in stats_list:
        print(
            f"连接 {stats.index:02d}：缓存 VLESS {stats.vless_lines} → 有效 {stats.valid_endpoints} "
            f"→ 唯一 {stats.unique_endpoints} → 前序重复 {stats.cross_subscription_duplicates} "
            f"→ 最终贡献 {stats.contributed_endpoints}"
        )
        print(
            f"  抓取 {stats.fetch_attempts}/{MAX_ROUNDS} 轮 · 新增缓存 {stats.cached_entries_added} · "
            f"过滤本机 {stats.discarded_blocked_lines} · 丢弃无效 {stats.discarded_invalid_lines}"
        )
    print("─" * 56)
    print(f"总计抓取轮次：{completed_rounds}/{MAX_ROUNDS}")
    print(f"缓存 VLESS 行数：{total_vless}")
    print(f"最终全局唯一节点：{len(nodes)}")
    print(f"输出文件：{OUTPUT_FILE}")
    print(f"统计文件：{STATS_FILE}")
    print("✓ 完整性：输出行数 = 全局唯一 HOST:PORT")
    print("✓ 过滤：127.0.0.1:1234 及无效 HOST:PORT 不进入输出缓存")
    print("✓ 缓存：仅本次运行内存；各订阅器分别去重，最终再进行全局去重；逐轮异常保留")
    if pending_sources:
        print(
            "⚠ 最多轮次结束后仍没有获得有效节点的订阅器（其余正常缓存仍已输出）："
            + "、".join(pending_sources)
        )
    if pending_panels:
        print(
            "⚠ 最多轮次结束后仍没有获得有效节点的未标注面板（其余正常缓存仍已输出）："
            + "、".join(pending_panels)
        )
    if not pending_sources and not pending_panels:
        print("✓ 所有来源均至少缓存了一个有效 HOST:PORT")
    print(f"✓ 订阅器名单：{SOURCE_LABEL_REGISTRY_ORIGIN}（{len(SOURCE_LABELS)} 个标签）")
    print("═" * 56)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
