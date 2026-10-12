"""Bounded, SSRF-resistant download of third-party images.

Search results and captured posts supply arbitrary URLs. Every hop is restricted to
HTTP(S) on the default ports, resolved once, rejected unless every address is public,
and then connected to that exact address so a second DNS answer cannot redirect the
request to a private network. Bodies are streamed under byte and time limits, and the
result must decode as a supported image within a pixel budget before it touches disk.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import socket
import tempfile
import time
from collections.abc import Callable, Iterable
from io import BytesIO
from pathlib import Path

import httpx
from PIL import Image

# Declared Content-Type -> Pillow format names accepted for that download.
IMAGE_FORMATS = {
    "image/jpeg": "JPEG",
    "image/png": "PNG",
    "image/webp": "WEBP",
    "image/gif": "GIF",
}
EXTENSIONS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}
DEFAULT_PORTS = {"http": 80, "https": 443}
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
NAT64_PREFIX = ipaddress.IPv6Network("64:ff9b::/96")

Resolver = Callable[[str, int], Iterable[str]]


class ImageDownloadError(ValueError):
    """A remote image was unsafe, unreachable, oversized, or not a valid image."""


def resolve_host(host: str, port: int) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise ImageDownloadError(f"could not resolve {host}") from exc
    return [str(info[4][0]) for info in infos]


def _embedded_ipv4(address: ipaddress.IPv6Address) -> list[ipaddress.IPv4Address]:
    embedded = [address.ipv4_mapped, address.sixtofour]
    if address.teredo:
        embedded.extend(address.teredo)
    if address in NAT64_PREFIX:
        embedded.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return [value for value in embedded if value is not None]


def is_public_address(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value.split("%", 1)[0])
    except ValueError:
        return False
    candidates: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = [address]
    if isinstance(address, ipaddress.IPv6Address):
        candidates.extend(_embedded_ipv4(address))
    return all(item.is_global and not item.is_multicast for item in candidates)


def _validated_url(raw: str | httpx.URL) -> httpx.URL:
    try:
        url = httpx.URL(raw)
    except (httpx.InvalidURL, TypeError) as exc:
        raise ImageDownloadError("invalid image URL") from exc
    if url.scheme not in DEFAULT_PORTS:
        raise ImageDownloadError("image URLs must use HTTP or HTTPS")
    if url.userinfo:
        raise ImageDownloadError("image URLs must not contain credentials")
    if not url.host:
        raise ImageDownloadError("image URL has no host")
    if url.port not in (None, DEFAULT_PORTS[url.scheme]):
        raise ImageDownloadError("image URLs must use the default HTTP or HTTPS port")
    return url


def _public_target(url: httpx.URL, resolver: Resolver) -> str:
    host = url.raw_host.decode("ascii")
    port = url.port or DEFAULT_PORTS[url.scheme]
    addresses = list(dict.fromkeys(resolver(host, port)))
    if not addresses:
        raise ImageDownloadError(f"could not resolve {host}")
    # Every answer must be public; otherwise a mixed answer set could still reach a private host.
    if not all(is_public_address(address) for address in addresses):
        raise ImageDownloadError("image host resolves to a non-public network address")
    return addresses[0].split("%", 1)[0]


def _validated_image(data: bytes, max_pixels: int) -> str:
    try:
        with Image.open(BytesIO(data)) as image:
            image_format = image.format or ""
            if image_format not in EXTENSIONS:
                raise ImageDownloadError(f"unsupported image format {image_format or 'unknown'}")
            width, height = image.size
            if width < 1 or height < 1 or width * height > max_pixels:
                raise ImageDownloadError("image dimensions exceed the configured pixel limit")
            # Decoding the first frame catches truncated or corrupt data before persistence.
            image.load()
    except ImageDownloadError:
        raise
    except (OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise ImageDownloadError("downloaded file is not a valid image") from exc
    return image_format


def _read_bounded(response: httpx.Response, max_bytes: int, deadline: float) -> bytes:
    declared = response.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > max_bytes:
        raise ImageDownloadError("image exceeds configured maximum size")
    chunks: list[bytes] = []
    total = 0
    for chunk in response.iter_bytes():
        total += len(chunk)
        if total > max_bytes:
            raise ImageDownloadError("image exceeds configured maximum size")
        if time.monotonic() > deadline:
            raise ImageDownloadError("image download exceeded its time limit")
        chunks.append(chunk)
    return b"".join(chunks)


def download_image(
    url: str,
    destination_dir: Path,
    *,
    max_bytes: int,
    max_pixels: int,
    allowed_types: Iterable[str] = IMAGE_FORMATS,
    timeout_seconds: float = 20.0,
    max_redirects: int = 3,
    resolver: Resolver = resolve_host,
    transport: httpx.BaseTransport | None = None,
) -> Path:
    """Download one public image into a content-addressed file and return its path."""

    allowed = frozenset(allowed_types)
    deadline = time.monotonic() + timeout_seconds * 2
    current = _validated_url(url)
    timeout = httpx.Timeout(timeout_seconds, connect=min(10.0, timeout_seconds))
    # trust_env=False keeps proxies and .netrc from bypassing the address checks.
    with httpx.Client(
        timeout=timeout, follow_redirects=False, trust_env=False, transport=transport
    ) as client:
        for _hop in range(max_redirects + 1):
            address = _public_target(current, resolver)
            request = client.build_request(
                "GET",
                current.copy_with(host=address),
                headers={
                    "Host": current.netloc.decode("ascii"),
                    "Accept": ", ".join(sorted(allowed)),
                    "Accept-Encoding": "identity",
                },
                # TLS still verifies the certificate against the original hostname.
                extensions={"sni_hostname": current.raw_host.decode("ascii")},
            )
            try:
                response = client.send(request, stream=True)
            except httpx.HTTPError as exc:
                raise ImageDownloadError(f"image request failed: {type(exc).__name__}") from exc
            try:
                if response.status_code in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise ImageDownloadError("image redirect has no location")
                    following = _validated_url(current.join(location))
                    if current.scheme == "https" and following.scheme != "https":
                        raise ImageDownloadError("image redirect downgrades HTTPS to HTTP")
                    current = following
                    continue
                if response.status_code != 200:
                    raise ImageDownloadError(f"image request returned HTTP {response.status_code}")
                content_type = response.headers.get("content-type", "").split(";")[0].strip()
                if content_type.casefold() not in allowed:
                    raise ImageDownloadError(f"unsupported MIME type {content_type or 'unknown'}")
                try:
                    data = _read_bounded(response, max_bytes, deadline)
                except httpx.HTTPError as exc:
                    raise ImageDownloadError(
                        f"image download failed: {type(exc).__name__}"
                    ) from exc
            finally:
                response.close()
            image_format = _validated_image(data, max_pixels)
            return _store(data, destination_dir, EXTENSIONS[image_format])
    raise ImageDownloadError("image URL redirected too many times")


def _store(data: bytes, destination_dir: Path, extension: str) -> Path:
    destination_dir.mkdir(parents=True, exist_ok=True)
    path = destination_dir / f"{hashlib.sha256(data).hexdigest()}{extension}"
    if path.exists():
        return path
    descriptor, temporary = tempfile.mkstemp(dir=destination_dir, suffix=".partial")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return path
