"""AWS Signature Version 4 for S3-compatible storage (Cloudflare R2).

Implements header signing and presigned (query-string) URLs as described in
https://docs.aws.amazon.com/AmazonS3/latest/API/sig-v4-authenticating-requests.html.
S3 rules apply: the canonical URI is the request path exactly as sent (already
percent-encoded once, never normalized), and presigned URLs use ``UNSIGNED-PAYLOAD``.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import quote, unquote, urlsplit, urlunsplit

ALGORITHM = "AWS4-HMAC-SHA256"
UNSIGNED_PAYLOAD = "UNSIGNED-PAYLOAD"
EMPTY_PAYLOAD_SHA256 = hashlib.sha256(b"").hexdigest()
# SigV4 itself caps X-Amz-Expires at seven days.
MAX_PRESIGN_SECONDS = 7 * 24 * 3600

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class SigV4Credentials:
    access_key_id: str
    secret_access_key: str = field(repr=False)


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def uri_encode(value: str, *, keep_slash: bool = False) -> str:
    """Percent-encode everything except the unreserved characters ``A-Za-z0-9-_.~``."""
    return quote(value, safe="-_.~/" if keep_slash else "-_.~")


def _amz_date(now: datetime) -> str:
    if now.tzinfo is None:
        raise ValueError("signing time must be timezone-aware")
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def _host(url: str) -> str:
    parts = urlsplit(url)
    if parts.hostname is None:
        raise ValueError("URL has no host")
    host = parts.hostname.lower()
    default_port = {"https": 443, "http": 80}.get(parts.scheme)
    if parts.port is not None and parts.port != default_port:
        host = f"{host}:{parts.port}"
    return host


def _query_pairs(query: str) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for item in query.split("&"):
        if not item:
            continue
        name, _, value = item.partition("=")
        pairs.append((unquote(name), unquote(value)))
    return pairs


def _canonical_query(pairs: list[tuple[str, str]]) -> str:
    encoded = sorted((uri_encode(name), uri_encode(value)) for name, value in pairs)
    return "&".join(f"{name}={value}" for name, value in encoded)


def _canonical_headers(headers: Mapping[str, str]) -> tuple[str, str]:
    normalized = {
        name.strip().lower(): _WHITESPACE_RE.sub(" ", value.strip())
        for name, value in headers.items()
    }
    names = sorted(normalized)
    canonical = "".join(f"{name}:{normalized[name]}\n" for name in names)
    return canonical, ";".join(names)


def canonical_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    payload_hash: str,
    *,
    extra_query: list[tuple[str, str]] | None = None,
) -> tuple[str, str]:
    """Return the canonical request and its signed-headers list."""
    parts = urlsplit(url)
    pairs = _query_pairs(parts.query) + list(extra_query or [])
    canonical_headers, signed_headers = _canonical_headers(headers)
    request = "\n".join(
        (
            method.upper(),
            parts.path or "/",
            _canonical_query(pairs),
            canonical_headers,
            signed_headers,
            payload_hash,
        )
    )
    return request, signed_headers


def _scope(date_stamp: str, region: str, service: str) -> str:
    return f"{date_stamp}/{region}/{service}/aws4_request"


def _signature(
    credentials: SigV4Credentials,
    amz_date: str,
    region: str,
    service: str,
    request: str,
) -> str:
    date_stamp = amz_date[:8]
    string_to_sign = "\n".join(
        (ALGORITHM, amz_date, _scope(date_stamp, region, service), sha256_hex(request.encode()))
    )
    key = f"AWS4{credentials.secret_access_key}".encode()
    for part in (date_stamp, region, service, "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    return hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()


def sign_headers(
    method: str,
    url: str,
    headers: Mapping[str, str],
    *,
    payload_hash: str,
    credentials: SigV4Credentials,
    region: str,
    service: str,
    now: datetime,
) -> dict[str, str]:
    """Return ``headers`` plus ``x-amz-date``, ``x-amz-content-sha256`` and ``Authorization``.

    ``Host`` is signed but not returned: the HTTP client sets it from the URL, and
    :func:`_host` derives it the same way (default ports omitted).
    """
    amz_date = _amz_date(now)
    sent = {name.lower(): value for name, value in headers.items() if name.lower() != "host"}
    sent["x-amz-date"] = amz_date
    sent["x-amz-content-sha256"] = payload_hash
    request, signed_headers = canonical_request(
        method, url, {**sent, "host": _host(url)}, payload_hash
    )
    signature = _signature(credentials, amz_date, region, service, request)
    scope = _scope(amz_date[:8], region, service)
    sent["authorization"] = (
        f"{ALGORITHM} Credential={credentials.access_key_id}/{scope},"
        f"SignedHeaders={signed_headers},Signature={signature}"
    )
    return sent


def presign_url(
    method: str,
    url: str,
    *,
    credentials: SigV4Credentials,
    region: str,
    service: str,
    now: datetime,
    expires_seconds: int,
    signed_headers: Mapping[str, str] | None = None,
) -> str:
    """Return ``url`` with query-string authentication.

    ``signed_headers`` (beyond ``host``) must be sent with exactly these values by whoever
    uses the URL; signing ``content-length`` is how a part URL carries its length.
    """
    if not 1 <= expires_seconds <= MAX_PRESIGN_SECONDS:
        raise ValueError(f"expires_seconds must be between 1 and {MAX_PRESIGN_SECONDS}")
    amz_date = _amz_date(now)
    headers = {name.lower(): value for name, value in (signed_headers or {}).items()}
    headers["host"] = _host(url)
    _, signed_names = _canonical_headers(headers)
    scope = _scope(amz_date[:8], region, service)
    auth_query = [
        ("X-Amz-Algorithm", ALGORITHM),
        ("X-Amz-Credential", f"{credentials.access_key_id}/{scope}"),
        ("X-Amz-Date", amz_date),
        ("X-Amz-Expires", str(expires_seconds)),
        ("X-Amz-SignedHeaders", signed_names),
    ]
    request, _ = canonical_request(method, url, headers, UNSIGNED_PAYLOAD, extra_query=auth_query)
    signature = _signature(credentials, amz_date, region, service, request)
    parts = urlsplit(url)
    query = _canonical_query(_query_pairs(parts.query) + auth_query)
    query = f"{query}&X-Amz-Signature={signature}"
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", query, ""))
