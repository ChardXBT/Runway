from __future__ import annotations

from collections.abc import Iterator
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from PIL import Image

from runway.media import download
from runway.media.download import ImageDownloadError, download_image, is_public_address

PUBLIC_IP = "93.184.215.14"
MAX_BYTES = 1024 * 1024


def png_bytes(size: tuple[int, int] = (64, 48), mode: str = "RGB") -> bytes:
    buffer = BytesIO()
    Image.new(mode, size, color=128 if mode == "L" else (200, 40, 40)).save(buffer, "PNG")
    return buffer.getvalue()


def resolver(table: dict[str, list[str]]) -> download.Resolver:
    def resolve(host: str, _port: int) -> list[str]:
        return table[host]

    return resolve


def fetch(
    url: str,
    handler: httpx.MockTransport,
    tmp_path: Path,
    *,
    hosts: dict[str, list[str]] | None = None,
    max_bytes: int = MAX_BYTES,
    max_pixels: int = 1_000_000,
) -> Path:
    return download_image(
        url,
        tmp_path / "downloads",
        max_bytes=max_bytes,
        max_pixels=max_pixels,
        resolver=resolver(hosts or {"images.example": [PUBLIC_IP]}),
        transport=handler,
    )


def test_downloads_to_a_content_addressed_file_through_the_pinned_address(
    tmp_path: Path,
) -> None:
    body = png_bytes()
    seen: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, headers={"content-type": "image/png"}, content=body)

    path = fetch("https://images.example/a.png?x=1", httpx.MockTransport(handle), tmp_path)

    assert path.read_bytes() == body
    assert path.suffix == ".png"
    assert seen[0].url.host == PUBLIC_IP
    assert seen[0].url.query == b"x=1"
    assert seen[0].headers["host"] == "images.example"
    assert seen[0].extensions["sni_hostname"] == "images.example"
    assert not list(path.parent.glob("*.partial"))


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.8",
        "172.16.4.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",
        "224.0.0.1",
        "::1",
        "fe80::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "64:ff9b::7f00:1",
        "2002:7f00:1::",
    ],
)
def test_non_public_addresses_are_refused(address: str) -> None:
    assert not is_public_address(address)


def test_public_addresses_are_allowed() -> None:
    assert is_public_address(PUBLIC_IP)
    assert is_public_address("2606:4700:4700::1111")


@pytest.mark.parametrize(
    "answers",
    [["127.0.0.1"], ["169.254.169.254"], [PUBLIC_IP, "10.0.0.1"], []],
)
def test_hosts_resolving_to_private_or_no_addresses_are_never_contacted(
    tmp_path: Path, answers: list[str]
) -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("a private address must not be contacted")

    with pytest.raises(ImageDownloadError):
        fetch(
            "http://rebind.example/a.png",
            httpx.MockTransport(handle),
            tmp_path,
            hosts={"rebind.example": answers},
        )


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("file:///etc/passwd", "HTTP or HTTPS"),
        ("ftp://images.example/a.png", "HTTP or HTTPS"),
        ("http://user:secret@images.example/a.png", "credentials"),
        ("http://images.example:8000/a.png", "default HTTP or HTTPS port"),
        ("not a url", "HTTP or HTTPS"),
    ],
)
def test_unsafe_urls_are_refused_before_any_request(tmp_path: Path, url: str, message: str) -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("an unsafe URL must not be requested")

    with pytest.raises(ImageDownloadError, match=message):
        fetch(url, httpx.MockTransport(handle), tmp_path)


def test_redirect_targets_are_revalidated(tmp_path: Path) -> None:
    requested: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        requested.append(request.headers["host"])
        return httpx.Response(302, headers={"location": "http://metadata.internal/latest"})

    with pytest.raises(ImageDownloadError, match="non-public"):
        fetch(
            "http://images.example/a.png",
            httpx.MockTransport(handle),
            tmp_path,
            hosts={"images.example": [PUBLIC_IP], "metadata.internal": ["169.254.169.254"]},
        )
    assert requested == ["images.example"]


def test_relative_redirects_are_followed_and_downgrades_refused(tmp_path: Path) -> None:
    body = png_bytes()

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old.png":
            return httpx.Response(301, headers={"location": "/new.png"})
        if request.url.path == "/insecure.png":
            return httpx.Response(302, headers={"location": "http://images.example/new.png"})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=body)

    transport = httpx.MockTransport(handle)
    assert fetch("https://images.example/old.png", transport, tmp_path).read_bytes() == body
    with pytest.raises(ImageDownloadError, match="downgrades"):
        fetch("https://images.example/insecure.png", transport, tmp_path)


def test_redirect_loops_are_bounded(tmp_path: Path) -> None:
    hops: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        hops.append(request.url.path)
        return httpx.Response(302, headers={"location": f"/hop{len(hops)}"})

    with pytest.raises(ImageDownloadError, match="too many"):
        fetch("http://images.example/start", httpx.MockTransport(handle), tmp_path)
    assert len(hops) == 4


def test_declared_oversized_body_is_refused_without_reading(tmp_path: Path) -> None:
    def body() -> Iterator[bytes]:
        raise AssertionError("an oversized body must not be read")
        yield b""  # pragma: no cover

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "image/png", "content-length": str(MAX_BYTES + 1)},
            content=body(),
        )

    with pytest.raises(ImageDownloadError, match="maximum size"):
        fetch("http://images.example/a.png", httpx.MockTransport(handle), tmp_path)


def test_undeclared_oversized_stream_stops_at_the_limit(tmp_path: Path) -> None:
    sent: list[int] = []

    def body() -> Iterator[bytes]:
        for _ in range(64):
            sent.append(1)
            yield b"\0" * 65536

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/png"}, content=body())

    with pytest.raises(ImageDownloadError, match="maximum size"):
        fetch("http://images.example/a.png", httpx.MockTransport(handle), tmp_path)
    assert len(sent) == MAX_BYTES // 65536 + 1
    assert not (tmp_path / "downloads").exists()


def test_slow_streams_hit_the_overall_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = iter(range(0, 1000, 15))
    monkeypatch.setattr(download.time, "monotonic", lambda: float(next(clock)))

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "image/png"}, content=iter([b"a"] * 100)
        )

    with pytest.raises(ImageDownloadError, match="time limit"):
        fetch("http://images.example/a.png", httpx.MockTransport(handle), tmp_path)


def test_decompression_bombs_are_refused_before_decoding(tmp_path: Path) -> None:
    bomb = png_bytes((4000, 4000), mode="L")
    assert len(bomb) < MAX_BYTES

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "image/png"}, content=bomb)

    with pytest.raises(ImageDownloadError, match="pixel limit"):
        fetch("http://images.example/a.png", httpx.MockTransport(handle), tmp_path)


@pytest.mark.parametrize(
    ("content_type", "body", "message"),
    [
        ("text/html", b"<html></html>", "unsupported MIME type"),
        ("image/png", b"definitely not an image", "not a valid image"),
        ("image/png", png_bytes((400, 400))[:200], "not a valid image"),
        ("image/png", b"<svg xmlns='http://www.w3.org/2000/svg'/>", "not a valid image"),
    ],
)
def test_malformed_or_mislabelled_content_is_refused(
    tmp_path: Path, content_type: str, body: bytes, message: str
) -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": content_type}, content=body)

    with pytest.raises(ImageDownloadError, match=message):
        fetch("http://images.example/a.png", httpx.MockTransport(handle), tmp_path)
    assert not (tmp_path / "downloads").exists()


def test_http_errors_are_reported_without_persisting(tmp_path: Path) -> None:
    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, headers={"content-type": "image/png"}, content=png_bytes())

    with pytest.raises(ImageDownloadError, match="HTTP 404"):
        fetch("http://images.example/a.png", httpx.MockTransport(handle), tmp_path)
