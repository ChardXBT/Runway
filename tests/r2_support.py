"""In-memory R2 (S3-compatible) fake behind ``httpx.MockTransport``. No network.

Shared by ``tests/unit/test_mediastore_r2.py`` (store-level behaviour) and the
hosted upload tests, which run the real ``R2Store`` against it. The fake
authenticates every request the way S3 does: it recomputes the SigV4 signature
of what httpx actually sent, so a URL or header the client mangles after
signing fails here.
"""

from __future__ import annotations

import hashlib
import re
import uuid
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode

import httpx

from runway.mediastore import R2Config, R2Store, make_media_key
from runway.mediastore.sigv4 import presign_url, sha256_hex, sign_headers
from runway.platforms.limits import MiB

ACCOUNT = "0123456789abcdef0123456789abcdef"
BUCKET = "runway-media"
HOST = f"{ACCOUNT}.r2.cloudflarestorage.com"
CONFIG = R2Config(
    account_id=ACCOUNT,
    bucket=BUCKET,
    access_key_id="test-access-key",
    secret_access_key="test-secret-key-not-real",
)
PART = 5 * MiB
WS = "ws1"
NS = "http://s3.amazonaws.com/doc/2006-03-01/"
START = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def _xml(tag: str, children: str) -> bytes:
    return f'<?xml version="1.0" encoding="UTF-8"?><{tag} xmlns="{NS}">{children}</{tag}>'.encode()


def _error(status: int, code: str) -> httpx.Response:
    return httpx.Response(status, content=_xml("Error", f"<Code>{code}</Code>"))


class FakeR2:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.objects: dict[str, bytes] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.signed_requests: list[httpx.Request] = []
        self.presigned_requests: list[httpx.Request] = []
        self.complete_error_in_body = False
        self.fail_with: int | None = None
        self.ignore_range = False
        self.blocks_sent = 0
        self.before_complete: Callable[[str], None] | None = None

    # Authentication ------------------------------------------------------------------

    def _check_header_signature(self, request: httpx.Request) -> bool:
        auth = request.headers.get("authorization", "")
        match = re.search(r"SignedHeaders=([^,]+)", auth)
        if match is None:
            return False
        names = match.group(1).split(";")
        payload_hash = request.headers["x-amz-content-sha256"]
        if payload_hash != sha256_hex(request.content):
            return False
        when = datetime.strptime(request.headers["x-amz-date"], "%Y%m%dT%H%M%SZ")
        extra = {
            name: request.headers[name]
            for name in names
            if name not in {"host", "x-amz-date", "x-amz-content-sha256"}
        }
        expected = sign_headers(
            request.method,
            str(request.url),
            extra,
            payload_hash=payload_hash,
            credentials=CONFIG.credentials,
            region="auto",
            service="s3",
            now=when.replace(tzinfo=UTC),
        )
        return bool(expected["authorization"] == auth and request.headers["host"] == HOST)

    def _check_presigned(self, request: httpx.Request, query: list[tuple[str, str]]) -> bool:
        params = dict(query)
        when = datetime.strptime(params["X-Amz-Date"], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        expires = int(params["X-Amz-Expires"])
        if self.clock.now > when + timedelta(seconds=expires):
            return False
        names = params["X-Amz-SignedHeaders"].split(";")
        headers = {name: request.headers.get(name, "") for name in names if name != "host"}
        plain = [(k, v) for k, v in query if not k.startswith("X-Amz-")]
        base = f"https://{HOST}{request.url.path}"
        if plain:
            base += "?" + urlencode(plain)
        expected = presign_url(
            request.method,
            base,
            credentials=CONFIG.credentials,
            region="auto",
            service="s3",
            now=when,
            expires_seconds=expires,
            signed_headers=headers,
        )
        return expected.endswith(f"X-Amz-Signature={params['X-Amz-Signature']}")

    # Dispatch ----------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        query = parse_qsl(request.url.query.decode(), keep_blank_values=True)
        params = dict(query)
        if "X-Amz-Signature" in params:
            self.presigned_requests.append(request)
            if not self._check_presigned(request, query):
                return _error(403, "SignatureDoesNotMatch")
        else:
            self.signed_requests.append(request)
            if not self._check_header_signature(request):
                return _error(403, "SignatureDoesNotMatch")
        if self.fail_with is not None:
            return _error(self.fail_with, "InternalError")

        path = unquote(request.url.path)
        assert path.startswith(f"/{BUCKET}")
        key = path[len(f"/{BUCKET}/") :]
        method = request.method
        if not key and "uploads" in params:
            return self._list_uploads(params)
        if method == "POST" and "uploads" in params:
            upload_id = uuid.uuid4().hex
            self.uploads[upload_id] = {"key": key, "parts": {}, "initiated": self.clock.now}
            return httpx.Response(
                200,
                content=_xml(
                    "InitiateMultipartUploadResult",
                    f"<Bucket>{BUCKET}</Bucket><Key>{key}</Key><UploadId>{upload_id}</UploadId>",
                ),
            )
        if "uploadId" in params:
            upload = self.uploads.get(params["uploadId"])
            if upload is None or upload["key"] != key:
                return _error(404, "NoSuchUpload")
            if method == "PUT":
                body = request.content
                etag = hashlib.md5(body).hexdigest()
                upload["parts"][int(params["partNumber"])] = (body, etag)
                return httpx.Response(200, headers={"etag": f'"{etag}"'})
            if method == "GET":
                return self._list_parts(upload, params)
            if method == "POST":
                if self.before_complete is not None:
                    hook, self.before_complete = self.before_complete, None
                    hook(params["uploadId"])
                    if params["uploadId"] not in self.uploads:
                        return _error(404, "NoSuchUpload")
                return self._complete(params["uploadId"], upload, request.content)
            if method == "DELETE":
                del self.uploads[params["uploadId"]]
                return httpx.Response(204)
        if method == "PUT":
            # A plain object write (a server-made object like a poster frame).
            self.objects[key] = request.content
            return httpx.Response(200, headers={"etag": self._etag(key)})
        if method == "HEAD":
            if key not in self.objects:
                return httpx.Response(404)
            data = self.objects[key]
            return httpx.Response(
                200, headers={"content-length": str(len(data)), "etag": self._etag(key)}
            )
        if method == "GET":
            if key not in self.objects:
                return _error(404, "NoSuchKey")
            data = self.objects[key]
            if "if-match" in request.headers and request.headers["if-match"] != self._etag(key):
                return _error(412, "PreconditionFailed")
            headers = {
                header: params[f"response-{header}"]
                for header in ("content-type", "content-disposition")
                if f"response-{header}" in params
            }
            if "range" not in request.headers or self.ignore_range:
                return httpx.Response(200, content=self._blocks(data), headers=headers)
            first, last = map(int, request.headers["range"][len("bytes=") :].split("-"))
            return httpx.Response(206, content=self._blocks(data[first : last + 1]))
        if method == "DELETE":
            self.objects.pop(key, None)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {method} {request.url}")

    def _blocks(self, data: bytes, size: int = 64 * 1024) -> Iterator[bytes]:
        for start in range(0, len(data), size):
            self.blocks_sent += 1
            yield data[start : start + size]

    def _etag(self, key: str) -> str:
        return f'"{hashlib.md5(self.objects[key]).hexdigest()}"'

    def _list_parts(self, upload: dict[str, Any], params: dict[str, str]) -> httpx.Response:
        marker = int(params.get("part-number-marker", "0"))
        limit = int(params.get("max-parts", "1000"))
        numbers = sorted(number for number in upload["parts"] if number > marker)
        page, rest = numbers[:limit], numbers[limit:]
        body = "".join(
            f"<Part><PartNumber>{number}</PartNumber><ETag>&quot;{upload['parts'][number][1]}"
            f"&quot;</ETag><Size>{len(upload['parts'][number][0])}</Size></Part>"
            for number in page
        )
        truncated = "true" if rest else "false"
        next_marker = f"<NextPartNumberMarker>{page[-1]}</NextPartNumberMarker>" if page else ""
        return httpx.Response(
            200,
            content=_xml(
                "ListPartsResult", f"<IsTruncated>{truncated}</IsTruncated>{next_marker}{body}"
            ),
        )

    def _complete(self, upload_id: str, upload: dict[str, Any], body: bytes) -> httpx.Response:
        if self.complete_error_in_body:
            return httpx.Response(200, content=_xml("Error", "<Code>InternalError</Code>"))
        root = ElementTree.fromstring(body)
        assembled = b""
        for part in root.findall("Part"):
            number = int(part.findtext("PartNumber") or "0")
            etag = (part.findtext("ETag") or "").strip('"')
            data, stored_etag = upload["parts"][number]
            if etag != stored_etag:
                return _error(400, "InvalidPart")
            assembled += data
        self.objects[upload["key"]] = assembled
        del self.uploads[upload_id]
        return httpx.Response(200, content=_xml("CompleteMultipartUploadResult", ""))

    def _list_uploads(self, params: dict[str, str]) -> httpx.Response:
        prefix = params.get("prefix", "")
        limit = int(params.get("max-uploads", "1000"))
        entries = sorted(
            (upload["key"], upload_id, upload["initiated"])
            for upload_id, upload in self.uploads.items()
            if upload["key"].startswith(prefix)
        )
        marker = (params.get("key-marker", ""), params.get("upload-id-marker", ""))
        if marker[0]:
            entries = [entry for entry in entries if (entry[0], entry[1]) > marker]
        page, rest = entries[:limit], entries[limit:]
        body = "".join(
            f"<Upload><Key>{key}</Key><UploadId>{upload_id}</UploadId>"
            f"<Initiated>{initiated.strftime('%Y-%m-%dT%H:%M:%S.000Z')}</Initiated></Upload>"
            for key, upload_id, initiated in page
        )
        markers = ""
        if rest:
            markers = (
                f"<NextKeyMarker>{page[-1][0]}</NextKeyMarker>"
                f"<NextUploadIdMarker>{page[-1][1]}</NextUploadIdMarker>"
            )
        truncated = "true" if rest else "false"
        return httpx.Response(
            200,
            content=_xml(
                "ListMultipartUploadsResult",
                f"<IsTruncated>{truncated}</IsTruncated>{markers}{body}",
            ),
        )


def fake_r2_store(fake: FakeR2, clock: Clock, *, part_size: int = PART) -> R2Store:
    """A real ``R2Store`` whose HTTP layer is the in-memory fake."""
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return R2Store(CONFIG, client=client, part_size=part_size, clock=clock)


def fake_r2_browser(fake: FakeR2) -> httpx.Client:
    """Stands in for the user's browser: no credentials, only the URLs it was given."""
    return httpx.Client(transport=httpx.MockTransport(fake.handler))


def _payload(size: int) -> bytes:
    block = bytes(range(256)) * 4096
    return (block * (size // len(block) + 1))[:size]


def _key(data: bytes, asset: str = "asset1") -> tuple[str, str]:
    digest = hashlib.sha256(data).hexdigest()
    return make_media_key("ws1", asset, digest, "mp4"), digest


def _upload_parts(
    browser: httpx.Client, urls: dict[int, str], data: bytes, part_size: int = PART
) -> None:
    for number, url in urls.items():
        chunk = data[(number - 1) * part_size : number * part_size]
        response = browser.put(url, content=chunk)
        assert response.status_code == 200, response.text
