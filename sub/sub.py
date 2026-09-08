#!/usr/bin/env python3
"""Fetch HTTP(S) VLESS subscriptions and write unique host:port endpoints."""

from __future__ import annotations

import base64
import binascii
import ipaddress
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


USER_AGENT = "v2rayNG/2.2.6"
OUTPUT_FILE = Path(__file__).resolve().with_name("sub.txt")

MAX_ATTEMPTS = max(1, int(os.environ.get("SUB_MAX_ATTEMPTS", "3")))
CONNECT_TIMEOUT = max(1.0, float(os.environ.get("SUB_CONNECT_TIMEOUT", "20")))
READ_TIMEOUT = max(1.0, float(os.environ.get("SUB_READ_TIMEOUT", "120")))
RETRY_DELAY = max(0.0, float(os.environ.get("SUB_RETRY_DELAY", "3")))
RETRY_BACKOFF = max(1.0, float(os.environ.get("SUB_RETRY_BACKOFF", "2")))

VLESS_PREFIX = "vless://"
BASE64_RE = re.compile(r"^[A-Za-z0-9+/\s=_-]+$")
ENDPOINT_RE = re.compile(r"^\[[^\[\]]+\]:\d{1,5}$|^[^:\s]+:\d{1,5}$")


def fail(message: str) -> "NoReturn":
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def get_subscription_urls() -> list[str]:
    """Read one HTTP(S) subscription URL per line from Secret SUB or CLI args."""
    raw = os.environ.get("SUB", "").strip()
    if not raw and len(sys.argv) > 1:
        raw = "\n".join(arg.strip() for arg in sys.argv[1:] if arg.strip())

    if not raw:
        fail("SUB is empty")

    urls = [line.strip() for line in raw.splitlines() if line.strip()]
    if not urls:
        fail("SUB contains no subscription URL")

    invalid = [url for url in urls if urlsplit(url).scheme.lower() not in {"http", "https"}]
    if invalid:
        fail("SUB contains a non-HTTP(S) URL")

    return urls


def safe_url(url: str) -> str:
    """Return a log-safe URL without query/fragment, which may contain secrets."""
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or parsed.netloc or "<unknown>"
        return f"{parsed.scheme}://{host}{parsed.path or '/'}"
    except ValueError:
        return "<invalid-url>"


def build_session() -> requests.Session:
    session = requests.Session()
    # Match the useful request characteristics from the supplied v2rayNG capture.
    # Clear Requests defaults so we do not add an unnecessary Accept header.
    session.headers.clear()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Connection": "close",
            "Accept-Encoding": "gzip",
        }
    )

    # Transport-level retries are limited to connection/routing failures and
    # selected transient HTTP statuses. Content validation remains our own logic.
    retry = Retry(
        total=0,
        connect=0,
        read=0,
        redirect=0,
        status=0,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=1, pool_maxsize=1)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def fetch(url: str, session: requests.Session) -> str:
    """Fetch one complete subscription with bounded application-level retries."""
    last_error: Exception | None = None

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            print(f"FETCH {attempt}/{MAX_ATTEMPTS}: {safe_url(url)}")
            with session.get(
                url,
                timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
                allow_redirects=True,
                stream=False,
            ) as response:
                response.raise_for_status()
                body = response.content  # fully consume the response before parsing

            if not body:
                raise ValueError("empty response body")

            text = decode_subscription_body(body)
            vless_count = count_vless_lines(text)
            if vless_count == 0:
                raise ValueError("response contains no VLESS line")

            print(f"  accepted: {vless_count} VLESS lines")
            return text

        except (requests.RequestException, ValueError, UnicodeError, binascii.Error) as exc:
            last_error = exc
            print(
                f"  attempt failed: {type(exc).__name__}: {redact(str(exc))}",
                file=sys.stderr,
            )
            if attempt < MAX_ATTEMPTS:
                delay = RETRY_DELAY * (RETRY_BACKOFF ** (attempt - 1))
                print(f"  retrying after {delay:g}s...", file=sys.stderr)
                time.sleep(delay)

    assert last_error is not None
    raise last_error


def decode_subscription_body(body: bytes) -> str:
    """Decode direct VLESS text or a single Base64-wrapped VLESS subscription."""
    text = body.decode("utf-8-sig", errors="strict")
    if has_vless(text):
        return text

    compact = re.sub(r"\s+", "", text)
    if not compact or len(compact) < 8 or not BASE64_RE.fullmatch(compact):
        raise ValueError("unsupported response format")

    normalized = compact.replace("-", "+").replace("_", "/")
    normalized += "=" * ((4 - len(normalized) % 4) % 4)
    decoded = base64.b64decode(normalized, validate=True)
    decoded_text = decoded.decode("utf-8-sig", errors="strict")
    if not has_vless(decoded_text):
        raise ValueError("Base64 response does not contain VLESS")
    return decoded_text


def has_vless(text: str) -> bool:
    return count_vless_lines(text) > 0


def count_vless_lines(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.lstrip().lower().startswith(VLESS_PREFIX))


def normalize_host(host: str) -> str:
    """Normalize only what is semantically equivalent for endpoint deduplication."""
    host = host.rstrip(".")
    try:
        return ipaddress.ip_address(host).compressed
    except ValueError:
        return host.lower()


def extract_host_port(line: str) -> str | None:
    """Extract only the VLESS authority's host:port portion."""
    line = line.strip()
    if not line.lower().startswith(VLESS_PREFIX):
        return None

    try:
        parsed = urlsplit(line)
        if parsed.scheme.lower() != "vless":
            return None
        if not parsed.username or not parsed.hostname:
            return None
        if parsed.port is None:
            return None

        host = normalize_host(parsed.hostname)
        port = parsed.port

        if ":" in host:
            endpoint = f"[{host}]:{port}"
        else:
            endpoint = f"{host}:{port}"

        if not ENDPOINT_RE.fullmatch(endpoint):
            return None
        return endpoint
    except ValueError:
        return None


def endpoint_key(endpoint: str) -> str:
    """Case-insensitive endpoint identity while preserving first-seen output text."""
    host, port = split_endpoint(endpoint)
    return f"{host.lower()}:{port}"


def split_endpoint(endpoint: str) -> tuple[str, int]:
    if endpoint.startswith("["):
        close = endpoint.find("]:")
        if close <= 0:
            raise ValueError("invalid IPv6 endpoint")
        return endpoint[1:close], int(endpoint[close + 2 :])
    host, sep, port = endpoint.rpartition(":")
    if not sep or not host:
        raise ValueError("invalid endpoint")
    return host, int(port)


def extract_nodes(texts: Iterable[str]) -> tuple[list[str], int]:
    """Return first-seen unique endpoints and the number of VLESS lines examined."""
    seen: set[str] = set()
    nodes: list[str] = []
    vless_lines = 0

    for text in texts:
        for line in text.splitlines():
            if not line.lstrip().lower().startswith(VLESS_PREFIX):
                continue
            vless_lines += 1
            endpoint = extract_host_port(line)
            if endpoint is None:
                continue
            key = endpoint_key(endpoint)
            if key not in seen:
                seen.add(key)
                nodes.append(endpoint)

    return nodes, vless_lines


def validate_output(nodes: list[str]) -> None:
    """Guarantee the generated file has exactly one valid non-empty line per node."""
    if not nodes:
        fail("no valid host:port nodes extracted; refusing to overwrite existing sub.txt")

    if len(set(endpoint_key(node) for node in nodes)) != len(nodes):
        fail("internal duplicate detected before output")

    for node in nodes:
        if not ENDPOINT_RE.fullmatch(node):
            fail(f"internal invalid endpoint: {node}")

    payload = "\n".join(nodes) + "\n"
    lines = payload.splitlines()
    if len(lines) != len(nodes) or any(not line.strip() for line in lines):
        fail("output line-count integrity check failed")


def write_output(nodes: list[str]) -> None:
    validate_output(nodes)

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = OUTPUT_FILE.with_name(OUTPUT_FILE.name + ".tmp")
    payload = "\n".join(nodes) + "\n"
    temp_file.write_text(payload, encoding="utf-8", newline="\n")
    temp_file.replace(OUTPUT_FILE)

    # Re-open and verify the exact on-disk result, not just the in-memory list.
    written = OUTPUT_FILE.read_text(encoding="utf-8")
    written_lines = written.splitlines()
    if written_lines != nodes:
        fail("on-disk output verification failed")
    if len(written_lines) != len(nodes):
        fail("on-disk output line-count mismatch")


def redact(value: str) -> str:
    return re.sub(r"https?://[^\s'\"]+", "<redacted-url>", value)


def main() -> int:
    urls = get_subscription_urls()
    print(f"SUBSCRIPTIONS: {len(urls)}")

    texts: list[str] = []
    session = build_session()

    try:
        for index, url in enumerate(urls, 1):
            try:
                texts.append(fetch(url, session))
            except Exception as exc:
                fail(
                    f"subscription {index} failed after {MAX_ATTEMPTS} attempts: "
                    f"{type(exc).__name__}: {redact(str(exc))}"
                )
    finally:
        session.close()

    nodes, vless_lines = extract_nodes(texts)
    write_output(nodes)

    print(f"VLESS LINES: {vless_lines}")
    print(f"UNIQUE HOST:PORT: {len(nodes)}")
    print(f"OUTPUT LINES: {len(nodes)}")
    print(f"OUTPUT: {OUTPUT_FILE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
