#!/usr/bin/env python3
"""Aggregate four subscription panels; link 3 is the tagged subscription panel."""
from __future__ import annotations

import base64
import binascii
import gzip
import hashlib
import io
import ipaddress
import os
import re
import shutil
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_FILE = BASE_DIR / "sub.txt"
STATS_FILE = BASE_DIR / "sub_stats.txt"

USER_AGENT = "v2rayNG/2.4.0"
REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Encoding": "gzip",
}
MAX_ROUNDS = 5  # 固定上限：最多五轮；符合恢复条件时仍可提前结束。
# Spool threshold is a RAM-to-disk transition, never a response-size limit.
SPOOL_MEMORY_THRESHOLD = 2 * 1024 * 1024
STREAM_CHUNK_BYTES = 256 * 1024
MIN_SOURCE_ENDPOINTS = 1
CONNECT_TIMEOUT = max(1.0, float(os.getenv("SUB_CONNECT_TIMEOUT", "20")))
READ_TIMEOUT = max(1.0, float(os.getenv("SUB_READ_TIMEOUT", "180")))
RETRY_DELAY = max(0.0, float(os.getenv("SUB_RETRY_DELAY", "5")))
RETRY_BACKOFF = max(1.0, float(os.getenv("SUB_RETRY_BACKOFF", "2")))
MAX_REDIRECTS = max(0, int(os.getenv("SUB_MAX_REDIRECTS", "5")))
BLOCKED_ENDPOINTS = {"127.0.0.1:1234"}
VLESS_PREFIX = "vless://"
BASE64_RE = re.compile(r"^[A-Za-z0-9+/=_-]+$")
HOSTNAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
LABEL_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?$")
ENDPOINT_RE = re.compile(r"^(?:\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9.-]+):([1-9][0-9]{0,4})$")
BESTCF_LABELS_FILE = BASE_DIR.parent / "bestcf" / "bestcf.txt"
DEFAULT_SOURCE_LABELS = (
    "CM", "Mia", "天诚1", "天诚2", "Moist_R", "洛璃", "辣子鸡",
    "辣椒炒肉少放辣", "S5公益", "青云志", "老王", "文烨", "Kristi",
    "周润发", "DanFeng",
)
SOURCE_LABELS = DEFAULT_SOURCE_LABELS
SOURCE_LABEL_SET = set(SOURCE_LABELS)
SOURCE_LABEL_REGISTRY_ORIGIN = "内置名单（回退）"
SOURCE_TAG_PATTERN = re.compile(r"\[([^\[\]]{1,96})\]")
SUB_URI_PATTERN = re.compile(r"sub://[^\s\"'<>]+", re.IGNORECASE)


@dataclass
class PanelStats:
    index: int
    requested_rounds: int = 0
    failed_rounds: int = 0
    raw_lines: int = 0
    valid_lines: int = 0
    invalid_lines: int = 0
    blocked_lines: int = 0
    response_duplicates: int = 0
    cached_duplicates: int = 0
    cache_added: int = 0
    elapsed_seconds: float = 0.0


def fail(message: str) -> "NoReturn":
    print(f"✖ {message}", file=sys.stderr)
    raise SystemExit(1)


def get_subscription_urls() -> list[str]:
    """Read the four URLs in the same order as bestcf's four panel inputs."""
    raw = os.getenv("SUB", "").strip()
    if not raw and len(sys.argv) > 1:
        raw = "\n".join(arg.strip() for arg in sys.argv[1:] if arg.strip())
    if not raw:
        fail("未检测到订阅地址，请配置 GitHub Secret：sub")
    urls = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(urls) != 4:
        fail(f"Secret sub 应按 bestcf 顺序提供 4 个面板链接，当前读取到 {len(urls)} 个")
    for index, url in enumerate(urls, 1):
        try:
            parsed = urlsplit(url)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                raise ValueError("必须是有效的 HTTP(S) URL")
            _ = parsed.port
        except ValueError as exc:
            fail(f"第 {index} 个订阅链接格式错误：{redact(str(exc))}")
    return urls


def safe_url_label(url: str) -> str:
    try:
        return urlsplit(url).hostname or "未知主机"
    except ValueError:
        return "未知主机"


def host_header_for(url: str) -> str | None:
    try:
        parsed = urlsplit(url)
        if not parsed.hostname:
            return None
        host = parsed.hostname
        try:
            if ipaddress.ip_address(host).version == 6:
                host = f"[{host}]"
        except ValueError:
            pass
        return f"{host}:{parsed.port}" if parsed.port is not None else host
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
    labels: list[str] = []
    seen: set[str] = set()
    for raw_line in content.splitlines():
        match = SUB_URI_PATTERN.search(raw_line)
        if not match:
            continue
        entry = match.group(0).rstrip("),;]}`")
        try:
            parsed = urlsplit(entry)
        except ValueError:
            continue
        if parsed.scheme.lower() != "sub" or not parsed.netloc:
            continue
        label = unquote(parsed.fragment).strip()
        if label and not any(ord(char) < 32 or ord(char) == 127 for char in label) and label not in seen:
            labels.append(label)
            seen.add(label)
    return tuple(labels)


def load_source_labels() -> tuple[str, ...]:
    """Use the checked-out bestcf.txt so classification matches this run's repo snapshot."""
    global SOURCE_LABEL_REGISTRY_ORIGIN
    try:
        content = BESTCF_LABELS_FILE.read_text(encoding="utf-8-sig", errors="strict")
        labels = extract_source_labels_from_bestcf(content)
        if not labels:
            raise ValueError("bestcf.txt 中没有找到有效 sub:// 标签")
        SOURCE_LABEL_REGISTRY_ORIGIN = "仓库本地 bestcf/bestcf.txt"
        return labels
    except (UnicodeError, ValueError, OSError) as exc:
        SOURCE_LABEL_REGISTRY_ORIGIN = "内置名单（回退）"
        print(
            f"⚠ 无法读取本地 bestcf/bestcf.txt，使用内置 {len(DEFAULT_SOURCE_LABELS)} 个订阅器标签："
            f"{type(exc).__name__}：{redact(str(exc))}",
            file=sys.stderr,
        )
        return DEFAULT_SOURCE_LABELS


def set_source_labels(labels: tuple[str, ...]) -> None:
    global SOURCE_LABELS, SOURCE_LABEL_SET
    SOURCE_LABELS = tuple(labels)
    SOURCE_LABEL_SET = set(labels)


def source_from_line(line: str) -> str | None:
    try:
        fragment = unquote(urlsplit(line.strip()).fragment)
    except ValueError:
        return None
    for match in SOURCE_TAG_PATTERN.finditer(fragment):
        label = match.group(1).strip()
        if label in SOURCE_LABEL_SET:
            return label
    return None


def request_headers_for(url: str) -> dict[str, str]:
    headers = dict(REQUEST_HEADERS)
    host_header = host_header_for(url)
    if host_header:
        headers["Host"] = host_header
    return headers


def _new_spool():
    """Create a spill-to-disk stream: large responses are accepted, not rejected."""
    return tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY_THRESHOLD, mode="w+b")


def _count_vless_in_spool(spool) -> int:
    """Validate UTF-8 and count VLESS lines without loading the whole body into RAM."""
    spool.seek(0)
    wrapper = io.TextIOWrapper(spool, encoding="utf-8-sig", errors="strict", newline=None)
    try:
        return sum(1 for line in wrapper if line.lstrip().lower().startswith(VLESS_PREFIX))
    finally:
        try:
            wrapper.detach()
        except (ValueError, OSError):
            pass
        spool.seek(0)


def _decode_base64_spool(source):
    """Stream Base64 decoding into a spill-to-disk file, retaining only a tiny carry."""
    decoded = _new_spool()
    carry = bytearray()
    whitespace = b" \t\r\n\v\f"
    source.seek(0)
    try:
        while True:
            chunk = source.read(STREAM_CHUNK_BYTES)
            if not chunk:
                break
            compact = chunk.translate(None, whitespace).replace(b"-", b"+").replace(b"_", b"/")
            carry.extend(compact)
            # Keep the final quartet aside because it may contain Base64 padding.
            complete = max(0, (len(carry) // 4 - 1) * 4)
            if complete:
                decoded.write(base64.b64decode(bytes(carry[:complete]), validate=True))
                del carry[:complete]
        if carry:
            tail = bytes(carry)
            if len(tail) % 4:
                tail += b"=" * (4 - len(tail) % 4)
            decoded.write(base64.b64decode(tail, validate=True))
        decoded.seek(0)
        return decoded
    except Exception:
        decoded.close()
        raise


def decode_subscription_spool(body):
    """Return a streaming UTF-8 text reader for plain, gzip or Base64 VLESS data."""
    payload = body
    payload.seek(0)
    if payload.read(2) == b"\x1f\x8b":
        payload.seek(0)
        decompressed = _new_spool()
        try:
            with gzip.GzipFile(fileobj=payload, mode="rb") as source:
                shutil.copyfileobj(source, decompressed, length=STREAM_CHUNK_BYTES)
            decompressed.seek(0)
        except (EOFError, OSError, zlib.error) as exc:
            decompressed.close()
            payload.close()
            raise ValueError("gzip 响应解压失败或数据不完整") from exc
        payload.close()
        payload = decompressed

    try:
        if _count_vless_in_spool(payload):
            payload.seek(0)
            return io.TextIOWrapper(payload, encoding="utf-8-sig", errors="strict", newline=None)
    except UnicodeDecodeError:
        # A Base64 response is ASCII; invalid UTF-8 is not a direct VLESS body.
        pass

    try:
        decoded = _decode_base64_spool(payload)
        if not _count_vless_in_spool(decoded):
            decoded.close()
            raise ValueError("Base64 解码成功，但内容没有发现 VLESS 节点")
        decoded.seek(0)
        payload.close()
        return io.TextIOWrapper(decoded, encoding="utf-8-sig", errors="strict", newline=None)
    except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
        payload.close()
        if isinstance(exc, ValueError) and "没有发现 VLESS" in str(exc):
            raise
        raise ValueError("响应既不是明文 VLESS，也不是可识别的 Base64 VLESS") from exc


def fetch(url: str, session: requests.Session):
    """Fetch one URL to a spill-to-disk stream; no hard response-size cap."""
    current_url = url
    for redirect_index in range(MAX_REDIRECTS + 1):
        with session.get(
            current_url,
            headers=request_headers_for(current_url),
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            allow_redirects=False,
            stream=True,
        ) as response:
            status = response.status_code
            location = response.headers.get("Location")
            if status in {301, 302, 303, 307, 308} and location:
                if redirect_index >= MAX_REDIRECTS:
                    raise ValueError("重定向次数超过上限")
                next_url = urljoin(current_url, location)
                parsed = urlsplit(next_url)
                if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
                    raise ValueError("重定向目标不是有效 HTTP(S) 地址")
                current_url = next_url
                continue
            if status < 200 or status >= 300:
                raise requests.HTTPError(f"HTTP {status}", response=response)

            body = _new_spool()
            try:
                for chunk in response.iter_content(chunk_size=STREAM_CHUNK_BYTES):
                    if chunk:
                        body.write(chunk)
                if body.tell() == 0:
                    raise ValueError("响应正文为空")
                body.seek(0)
                return decode_subscription_spool(body)
            except Exception:
                body.close()
                raise
    raise ValueError("重定向处理失败")

def sleep_full_delay(seconds: float) -> None:
    """Wait the complete requested delay without passing huge values to one sleep call."""
    remaining = max(0.0, seconds)
    while remaining > 0:
        interval = min(remaining, 3600.0)
        started = time.monotonic()
        time.sleep(interval)
        elapsed = max(interval, time.monotonic() - started)
        remaining -= elapsed


def retry_delay(attempt: int, retry_after: str | None) -> float:
    """Respect a valid Retry-After value; otherwise use the existing backoff."""
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
            return max(0.0, (retry_at.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            pass
    return RETRY_DELAY * (RETRY_BACKOFF ** (attempt - 1))


def decode_subscription_body(body: bytes | bytearray) -> str:
    """Compatibility helper for tests/tools; production fetch uses streaming spools."""
    spool = _new_spool()
    spool.write(body)
    spool.seek(0)
    text_stream = decode_subscription_spool(spool)
    try:
        return text_stream.read()
    finally:
        text_stream.close()


def iter_subscription_lines(payload):
    if isinstance(payload, str):
        yield from io.StringIO(payload)
    else:
        yield from payload

def has_vless(text: str) -> bool:
    return count_vless_lines(text) > 0


def count_vless_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.lstrip().lower().startswith(VLESS_PREFIX))


def normalize_host(host: str) -> str | None:
    host = host.rstrip(".")
    if not host:
        return None
    try:
        return ipaddress.ip_address(host).compressed
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
    line = line.strip()
    if not line.lower().startswith(VLESS_PREFIX):
        return None
    try:
        parsed = urlsplit(line)
        if parsed.scheme.lower() != "vless" or parsed.hostname is None or parsed.port is None:
            return None
        if not 1 <= parsed.port <= 65535:
            return None
        host = normalize_host(parsed.hostname)
        if host is None:
            return None
        endpoint = f"[{host}]:{parsed.port}" if ":" in host else f"{host}:{parsed.port}"
        return endpoint if ENDPOINT_RE.fullmatch(endpoint) else None
    except ValueError:
        return None


def split_endpoint(endpoint: str) -> tuple[str, int]:
    if endpoint.startswith("["):
        close = endpoint.find("]:")
        return endpoint[1:close], int(endpoint[close + 2:])
    host, _, port = endpoint.rpartition(":")
    return host, int(port)


def endpoint_key(endpoint: str) -> str:
    """extract_host_port already returns canonical host:port, so reuse it as key."""
    return endpoint


def make_panel_cache(index: int) -> dict[str, object]:
    if index == 3:
        return {
            "kind": "tagged",
            "sources": {label: {} for label in SOURCE_LABELS},
            "unlabelled": {},
            # Keep a link-level insertion-ordered index so every request does not
            # rebuild a union of all provider buckets just to count or merge nodes.
            "link_nodes": {},
            "cross_bucket_duplicates": 0,
        }
    return {"kind": "ordinary", "nodes": {}}


def cache_panel_response(index: int, text, cache: dict[str, object], panel_stats: PanelStats,
                          source_totals: dict[str, dict[str, int]],
                          unlabelled_totals: dict[str, int]) -> dict[str, object]:
    """Cache one link response; only link 3 has provider-specific sub-caches."""
    tagged = index == 3
    seen_by_bucket: dict[str | None, set[str]] = {}
    metrics: dict[str | None, dict[str, int]] = {}
    # Ordinary links have only one bucket, so its per-response set is already
    # the link-level unique set. Link 3 needs a separate cross-provider set.
    link_seen: set[str] | None = set() if tagged else None
    added = 0
    cross_bucket_added = 0

    for line in iter_subscription_lines(text):
        if not line.lstrip().lower().startswith(VLESS_PREFIX):
            continue
        source = source_from_line(line) if tagged else None
        metric = metrics.setdefault(source, {
            "raw": 0, "valid": 0, "invalid": 0, "blocked": 0,
            "round_unique": 0, "added": 0, "response_duplicates": 0,
            "cached_duplicates": 0,
        })
        metric["raw"] += 1
        panel_stats.raw_lines += 1
        if tagged and source is not None:
            source_totals[source]["raw"] += 1
        elif tagged:
            unlabelled_totals["raw"] += 1

        endpoint = extract_host_port(line)
        if endpoint is None:
            metric["invalid"] += 1
            panel_stats.invalid_lines += 1
            if tagged and source is not None:
                source_totals[source]["invalid"] += 1
            elif tagged:
                unlabelled_totals["invalid"] += 1
            continue
        if endpoint in BLOCKED_ENDPOINTS:
            metric["blocked"] += 1
            panel_stats.blocked_lines += 1
            if tagged and source is not None:
                source_totals[source]["blocked"] += 1
            elif tagged:
                unlabelled_totals["blocked"] += 1
            continue

        key = endpoint_key(endpoint)
        metric["valid"] += 1
        panel_stats.valid_lines += 1
        if tagged and source is not None:
            source_totals[source]["valid"] += 1
        elif tagged:
            unlabelled_totals["valid"] += 1

        bucket_seen = seen_by_bucket.setdefault(source, set())
        if key in bucket_seen:
            metric["response_duplicates"] += 1
            panel_stats.response_duplicates += 1
            continue
        bucket_seen.add(key)
        metric["round_unique"] += 1
        if link_seen is not None:
            link_seen.add(key)

        if tagged:
            bucket = cache["sources"].setdefault(source, {}) if source is not None else cache["unlabelled"]
            if key in bucket:
                metric["cached_duplicates"] += 1
                panel_stats.cached_duplicates += 1
            else:
                if key in cache["link_nodes"]:
                    cross_bucket_added += 1
                else:
                    cache["link_nodes"][key] = None
                bucket[key] = None
                metric["added"] += 1
                panel_stats.cache_added += 1
                added += 1
        else:
            bucket = cache["nodes"]
            if key in bucket:
                metric["cached_duplicates"] += 1
                panel_stats.cached_duplicates += 1
            else:
                bucket[key] = None
                metric["added"] += 1
                panel_stats.cache_added += 1
                added += 1

    if tagged:
        cache["cross_bucket_duplicates"] += cross_bucket_added
    return {
        "metrics": metrics,
        "link_round_unique": len(link_seen) if link_seen is not None else len(seen_by_bucket.get(None, ())),
        "added": added,
        "cross_bucket_duplicates_added": cross_bucket_added,
        "cross_bucket_duplicates": cache.get("cross_bucket_duplicates", 0),
    }


def build_tagged_link_nodes(cache: dict[str, object]) -> dict[str, None]:
    """Return the already-maintained link-3 union in first-seen order."""
    return cache["link_nodes"]


def panel_nodes(cache: dict[str, object]) -> dict[str, None]:
    return build_tagged_link_nodes(cache) if cache["kind"] == "tagged" else cache["nodes"]


def merge_all_panels(panel_caches: list[dict[str, object]]) -> tuple[dict[str, None], list[dict[str, int]]]:
    merged: dict[str, None] = {}
    summaries: list[dict[str, int]] = []
    for index, cache in enumerate(panel_caches, 1):
        local_nodes = panel_nodes(cache)
        prior_count = len(merged)
        overlap = 0
        for endpoint in local_nodes:
            if endpoint in merged:
                overlap += 1
            else:
                merged[endpoint] = None
        summaries.append({
            "index": index,
            "panel_unique": len(local_nodes),
            "new_global": len(merged) - prior_count,
            "overlap": overlap,
        })
    return merged, summaries


def cache_size(cache: dict[str, object]) -> int:
    return len(panel_nodes(cache))


def write_stats_only(stats_content: str) -> None:
    """Update the run report without touching the previous successful sub.txt."""
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", newline="\n", dir=BASE_DIR,
        prefix=f".{STATS_FILE.name}.", suffix=".tmp", delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        handle.write(stats_content.rstrip() + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temp_path, STATS_FILE)
    finally:
        temp_path.unlink(missing_ok=True)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_results(nodes, stats_content: str) -> None:
    """Stream sub.txt to disk and update its run statistics as a recoverable pair."""
    if not nodes:
        fail("四个面板均未取得有效 HOST:PORT，保留旧 sub.txt")

    staged: dict[Path, Path] = {}
    replaced: list[Path] = []
    with tempfile.TemporaryDirectory(prefix=".sub-stage-", dir=BASE_DIR) as temp_name:
        stage_dir = Path(temp_name)
        backups: dict[Path, Path | None] = {}
        for destination in (OUTPUT_FILE, STATS_FILE):
            if destination.exists():
                backup = stage_dir / f"backup-{destination.name}"
                shutil.copy2(destination, backup)
                backups[destination] = backup
            else:
                backups[destination] = None

        output_stage = stage_dir / OUTPUT_FILE.name
        stats_stage = stage_dir / STATS_FILE.name
        count = 0
        # nodes is the insertion-ordered key view of the final merge dictionary;
        # it is already unique, so do not allocate a second full-size set here.
        with output_stage.open("w", encoding="utf-8", newline="\n", buffering=1024 * 1024) as handle:
            for node in nodes:
                if node in BLOCKED_ENDPOINTS or not ENDPOINT_RE.fullmatch(node):
                    fail(f"最终输出包含无效或被过滤的 endpoint：{node}")
                handle.write(node)
                handle.write("\n")
                count += 1
            handle.flush()
            os.fsync(handle.fileno())
        if not count:
            fail("最终合并结果为空，保留旧 sub.txt")
        with stats_stage.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(stats_content.rstrip() + "\n")
            handle.flush()
            os.fsync(handle.fileno())

        staged[OUTPUT_FILE] = output_stage
        staged[STATS_FILE] = stats_stage
        expected = {destination: _file_sha256(path) for destination, path in staged.items()}
        try:
            for destination, path in staged.items():
                path.replace(destination)
                replaced.append(destination)
            for destination in staged:
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
                    f"sub 输出失败：{write_error}；回滚失败：{'；'.join(rollback_errors)}"
                ) from write_error
            raise

def redact(value: str) -> str:
    return re.sub(r"https?://[^\s'\"]+", "<已隐藏URL>", value)


def render_stats(
    urls: list[str],
    panel_stats: list[PanelStats],
    panel_caches: list[dict[str, object]],
    source_totals: dict[str, dict[str, int]],
    unlabelled_totals: dict[str, int],
    round_history: list[dict[str, object]],
    panel_merge: list[dict[str, int]],
    final_count: int,
    elapsed_seconds: float = 0.0,
) -> str:
    """Build a human-readable report: overview first, details second."""
    rule = "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
    recovered = sum(
        bool(panel_caches[2]["sources"].get(label)) for label in SOURCE_LABELS
    )
    pending = round_history[-1]["pending"] if round_history else list(SOURCE_LABELS)
    if not pending:
        finish_reason = "已知订阅器全部取得有效缓存，提前结束"
    elif len(round_history) >= MAX_ROUNDS:
        finish_reason = f"达到 {MAX_ROUNDS} 轮上限，保留本次已取得的全部有效缓存"
    else:
        finish_reason = "运行结束"

    lines = [
        rule,
        "SUB 订阅聚合 · 本次运行报告",
        rule,
        f"运行时间：{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}",
        f"运行耗时：{elapsed_seconds:.1f} 秒",
        f"运行轮次：{len(round_history)}/{MAX_ROUNDS} 轮",
        f"订阅器恢复：{recovered}/{len(SOURCE_LABELS)} 个已知订阅器",
        f"最终结果：{final_count:,} 个唯一 HOST:PORT",
        f"结束原因：{finish_reason}",
        "",
        "【一、四个输入面板】",
        "每轮均按 1 → 2 → 3 → 4 请求；仅第 3 个面板的已知订阅器恢复情况决定是否提前结束。",
        "缓存只在本次运行内累积，不跨 Actions 运行复用。",
    ]
    for item, url in zip(panel_stats, urls):
        role = "订阅面板（按订阅器分类）" if item.index == 3 else "独立面板（链接内去重）"
        unique_count = cache_size(panel_caches[item.index - 1])
        lines.append(
            f"  面板 {item.index}｜{role}｜{safe_url_label(url)}｜"
            f"请求 {item.requested_rounds} 次｜失败 {item.failed_rounds} 次｜"
            f"缓存 {unique_count:,} 条｜耗时 {item.elapsed_seconds:.1f} 秒"
        )

    lines.extend(["", "【二、逐轮请求明细】"])
    for round_item in round_history:
        lines.append(f"● 第 {round_item['round']}/{MAX_ROUNDS} 轮")
        for index, result in enumerate(round_item["links"], 1):
            elapsed = float(result.get("elapsed", 0.0))
            if not result["ok"]:
                lines.append(
                    f"  面板 {index}｜失败｜耗时 {elapsed:.1f} 秒｜"
                    f"{result['error']}｜之前缓存保留"
                )
                continue
            metric = result["metric"]
            lines.append(
                f"  面板 {index}｜原始 {metric['raw']:,}｜有效 {metric['valid']:,}｜"
                f"本轮唯一 {metric['round_unique']:,}｜新增 {metric['added']:,}｜"
                f"重复 {metric['response_duplicates'] + metric['cached_duplicates']:,}｜"
                f"无效 {metric['invalid']:,}｜过滤本机 {metric['blocked']:,}｜"
                f"累计 {metric['cumulative']:,}｜耗时 {elapsed:.1f} 秒"
            )
            if index == 3:
                lines.append("    订阅器｜本轮原始｜本轮唯一｜新增缓存｜累计缓存")
                for label in SOURCE_LABELS:
                    provider = result["sources"].get(label, {})
                    cached = len(panel_caches[2]["sources"].get(label, {}))
                    lines.append(
                        f"    {label}｜{provider.get('raw', 0):>6}｜"
                        f"{provider.get('round_unique', 0):>6}｜"
                        f"{provider.get('added', 0):>6}｜{cached:>6}"
                    )
                unknown = result.get("unlabelled", {})
                if unknown.get("raw", 0):
                    lines.append(
                        f"    未识别标签｜原始 {unknown['raw']}｜"
                        f"本轮唯一 {unknown['round_unique']}｜新增 {unknown['added']}"
                    )
        missing = round_item["pending"]
        lines.append("  尚未恢复：" + ("、".join(missing) if missing else "无，全部已恢复"))
        lines.append("")

    lines.extend([rule, "【三、第 3 面板订阅器累计情况】"])
    for label in SOURCE_LABELS:
        cached = len(panel_caches[2]["sources"].get(label, {}))
        metric = source_totals[label]
        status = "✓ 已恢复" if cached >= MIN_SOURCE_ENDPOINTS else "○ 暂无有效缓存"
        lines.append(
            f"  {status}｜{label}｜缓存唯一 {cached:,}｜"
            f"累计有效 {metric['valid']:,}｜无效 {metric['invalid']:,}｜"
            f"过滤本机 {metric['blocked']:,}"
        )

    tagged_cache = panel_caches[2]
    provider_cache_rows = sum(len(bucket) for bucket in tagged_cache["sources"].values())
    unlabelled_cache_rows = len(tagged_cache["unlabelled"])
    link3_unique = len(build_tagged_link_nodes(tagged_cache))
    lines.extend([
        f"  分类缓存：{provider_cache_rows + unlabelled_cache_rows:,} 条",
        f"  第 3 面板合并去重：{link3_unique:,} 个",
        f"  跨订阅器/未标注重复：{provider_cache_rows + unlabelled_cache_rows - link3_unique:,} 个",
        f"  未识别标签：原始 {unlabelled_totals['raw']:,}｜有效 {unlabelled_totals['valid']:,}｜"
        f"无效 {unlabelled_totals['invalid']:,}｜过滤本机 {unlabelled_totals['blocked']:,}｜"
        f"缓存唯一 {unlabelled_cache_rows:,}",
        "",
        rule,
        "【四、四面板最终合并】",
    ])
    for summary in panel_merge:
        stats = panel_stats[summary["index"] - 1]
        lines.append(
            f"  面板 {summary['index']}｜本链接唯一 {summary['panel_unique']:,}｜"
            f"新增到总表 {summary['new_global']:,}｜与前面面板重复 {summary['overlap']:,}"
        )
    lines.extend([
        "",
        rule,
        "【五、最终输出】",
        f"最终唯一 HOST:PORT：{final_count:,}",
        "输出文件：sub.txt",
        "统计文件：sub_stats.txt",
        "去重顺序：第 1 → 第 2 → 第 3 → 第 4 面板；不设置来源质量优先级。",
        rule,
    ])
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    run_started = time.monotonic()
    print("╭────────────────────────────────────────────────────────╮")
    print("│ SUB · 四面板订阅聚合                                    │")
    print("╰────────────────────────────────────────────────────────╯")
    print("面板 1、2、4：各自独立缓存与去重")
    print("面板 3：按订阅器分类缓存，面板内合并去重")
    print(f"每轮按 1 → 2 → 3 → 4 请求全部链接；最多 {MAX_ROUNDS} 轮。")
    try:
        urls = get_subscription_urls()
    except SystemExit as exc:
        try:
            write_stats_only(
                "订阅聚合运行报告\n"
                f"运行时间：{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                "运行结果：未能读取四个面板链接配置。\n"
                "请检查 GitHub Secret sub 是否仍按 bestcf 的顺序提供四行 URL。\n"
            )
        except OSError as log_error:
            print(f"订阅配置失败，统计日志也无法更新：{log_error}", file=sys.stderr)
        raise exc
    session = build_session()
    panel_stats = [PanelStats(index) for index in range(1, 5)]
    panel_caches: list[dict[str, object]] = []
    round_history: list[dict[str, object]] = []
    source_totals: dict[str, dict[str, int]] = {}
    unlabelled_totals = {"raw": 0, "valid": 0, "invalid": 0, "blocked": 0}

    try:
        set_source_labels(load_source_labels())
        source_totals = {
            label: {"raw": 0, "valid": 0, "invalid": 0, "blocked": 0}
            for label in SOURCE_LABELS
        }
        panel_caches = [make_panel_cache(index) for index in range(1, 5)]

        print(f"订阅器名单：{len(SOURCE_LABELS)} 个（来源：{SOURCE_LABEL_REGISTRY_ORIGIN}）")
        for index, url in enumerate(urls, 1):
            print(f"  连接 {index}：{safe_url_label(url)}" + ("（sub 订阅面板）" if index == 3 else ""))

        for round_number in range(1, MAX_ROUNDS + 1):
            print(f"\n========== 第 {round_number}/{MAX_ROUNDS} 轮 ==========")
            link_results: list[dict[str, object]] = []
            round_retry_after: list[float] = []
            for index, (url, stats, cache) in enumerate(zip(urls, panel_stats, panel_caches), 1):
                stats.requested_rounds += 1
                request_started = time.monotonic()
                print(f"  ├─ 面板 {index}/4：请求 {safe_url_label(url)}")
                text_stream = None
                try:
                    text_stream = fetch(url, session)
                except Exception as exc:
                    stats.failed_rounds += 1
                    request_elapsed = time.monotonic() - request_started
                    stats.elapsed_seconds += request_elapsed
                    response = getattr(exc, "response", None)
                    retry_value = response.headers.get("Retry-After") if response is not None else None
                    if retry_value:
                        round_retry_after.append(retry_delay(round_number, retry_value))
                    message = f"{type(exc).__name__}: {redact(str(exc))}"
                    print(f"  │ ✗ 请求失败｜{request_elapsed:.1f} 秒｜历史缓存保留：{message}", file=sys.stderr)
                    link_results.append({"ok": False, "error": message, "elapsed": request_elapsed})
                    continue

                try:
                    result = cache_panel_response(
                        index, text_stream, cache, stats, source_totals, unlabelled_totals
                    )
                finally:
                    if hasattr(text_stream, "close"):
                        text_stream.close()
                request_elapsed = time.monotonic() - request_started
                stats.elapsed_seconds += request_elapsed
                metric_by_source = result["metrics"]
                all_metrics = list(metric_by_source.values())
                metric = {
                    "raw": sum(item["raw"] for item in all_metrics),
                    "valid": sum(item["valid"] for item in all_metrics),
                    "invalid": sum(item["invalid"] for item in all_metrics),
                    "blocked": sum(item["blocked"] for item in all_metrics),
                    "round_unique": result["link_round_unique"],
                    "added": result["added"],
                    "response_duplicates": sum(item["response_duplicates"] for item in all_metrics),
                    "cached_duplicates": sum(item["cached_duplicates"] for item in all_metrics),
                    "cumulative": cache_size(cache),
                    "cross_bucket_duplicates": result["cross_bucket_duplicates"],
                    "elapsed": request_elapsed,
                }
                print(
                    f"  ✓ 原始 {metric['raw']}；有效 {metric['valid']}；本轮链接唯一 {metric['round_unique']}；"
                    f"新增缓存 {metric['added']}；累计 {metric['cumulative']}；"
                    f"无效 {metric['invalid']}；过滤本机 {metric['blocked']}；用时 {request_elapsed:.1f} 秒"
                )
                source_details: dict[str, dict[str, int]] = {}
                unlabelled: dict[str, int] = {}
                if index == 3:
                    for label in SOURCE_LABELS:
                        item = metric_by_source.get(label, {})
                        source_details[label] = {
                            "raw": item.get("raw", 0),
                            "round_unique": item.get("round_unique", 0),
                            "added": item.get("added", 0),
                        }
                        print(
                            f"    {label}：原始 {source_details[label]['raw']}；"
                            f"本轮唯一 {source_details[label]['round_unique']}；"
                            f"新增 {source_details[label]['added']}；"
                            f"累计 {len(cache['sources'].get(label, {}))}"
                        )
                    unknown = metric_by_source.get(None, {})
                    unlabelled = {
                        "raw": unknown.get("raw", 0),
                        "round_unique": unknown.get("round_unique", 0),
                        "added": unknown.get("added", 0),
                    }
                    if unlabelled["raw"]:
                        print(f"    未识别标签：原始 {unlabelled['raw']}；本轮唯一 {unlabelled['round_unique']}；新增 {unlabelled['added']}")
                    if index == 3:
                        classified_total = sum(len(bucket) for bucket in cache["sources"].values()) + len(cache["unlabelled"])
                        print(
                            f"    第 3 链接分类缓存合计 {classified_total} 条；"
                            f"链接级合并唯一 {metric['cumulative']} 个；"
                            f"累计跨标签/未标注重复 {metric['cross_bucket_duplicates']} 个"
                        )
                link_results.append({"ok": True, "metric": metric, "sources": source_details,
                                     "unlabelled": unlabelled,
                                     "cross_bucket_duplicates": metric["cross_bucket_duplicates"]})

            pending = [label for label in SOURCE_LABELS if not panel_caches[2]["sources"].get(label)]
            round_history.append({"round": round_number, "links": link_results, "pending": pending})
            print(f"\n第 3 面板订阅器状态：{len(SOURCE_LABELS)-len(pending)}/{len(SOURCE_LABELS)} 已取得有效缓存")
            if not pending:
                print("✓ 第 3 面板所有已知订阅器均有有效缓存，提前结束。")
                break
            if round_number < MAX_ROUNDS:
                print("仍未恢复：" + "、".join(pending))
                delays = round_retry_after
                delay = max(delays) if delays else retry_delay(round_number, None)
                print(f"{delay:g} 秒后开始下一轮；下一轮仍会请求全部四个链接。")
                if delay:
                    sleep_full_delay(delay)
            else:
                print(f"已完成 {MAX_ROUNDS} 轮；保留所有链接和订阅器已有的有效缓存。")

        nodes, panel_merge = merge_all_panels(panel_caches)
        elapsed_seconds = time.monotonic() - run_started
        stats_content = render_stats(
            urls, panel_stats, panel_caches, source_totals, unlabelled_totals,
            round_history, panel_merge, len(nodes), elapsed_seconds
        )
        if not nodes:
            write_stats_only(
                stats_content + "\n运行结果：四个链接均未提供可用节点；本次保留上一次 sub.txt。\n"
            )
            fail("四个链接均未取得有效 HOST:PORT，已更新本次统计日志并保留旧 sub.txt")
        write_results(nodes.keys(), stats_content)
        print("\n━━━━━━━━━━━━━━━━ 最终合并 ━━━━━━━━━━━━━━━━")
        for item in panel_merge:
            print(
                f"连接 {item['index']}：链接唯一 {item['panel_unique']}；新增全局 {item['new_global']}；"
                f"与前面链接重复 {item['overlap']}"
            )
        print(f"最终唯一 HOST:PORT：{len(nodes):,}")
        print(f"总耗时：{elapsed_seconds:.1f} 秒")
        print(f"详细报告：{STATS_FILE}")
        print("✓ SUB 订阅聚合完成")
        return 0
    except Exception as exc:
        # Unexpected parsing/cache/output errors must replace the previous report too.
        # Keep the last successful sub.txt untouched; only update the diagnostic report.
        try:
            if panel_caches and source_totals:
                current_nodes, current_merge = merge_all_panels(panel_caches)
                failure_report = render_stats(
                    urls, panel_stats, panel_caches, source_totals, unlabelled_totals,
                    round_history, current_merge, len(current_nodes),
                    time.monotonic() - run_started,
                )
            else:
                failure_report = (
                    "SUB 四面板订阅聚合运行报告\n"
                    f"运行时间：{datetime.now(timezone.utc).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}\n"
                )
            failure_report += (
                "\n【运行结果】\n"
                f"✗ 运行失败：{type(exc).__name__}: {redact(str(exc))}\n"
                "本次没有完成新的 sub.txt；上一份成功结果保持不变。\n"
            )
            write_stats_only(failure_report)
        except Exception as log_error:
            print(
                f"订阅聚合失败，更新 sub_stats.txt 也失败：{type(log_error).__name__}: {log_error}",
                file=sys.stderr,
            )
        print(f"\n✗ SUB 订阅聚合失败：{type(exc).__name__}: {redact(str(exc))}", file=sys.stderr)
        raise
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
