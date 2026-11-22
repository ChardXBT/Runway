"""R2Store against an in-memory S3 fake behind ``httpx.MockTransport``. No network.

The fake authenticates every request the way S3 does: it recomputes the SigV4 signature of
what httpx actually sent, so a URL or header the client mangles after signing fails here.
"""

from __future__ import annotations

import hashlib
import re
import uuid
import xml.etree.ElementTree as ElementTree
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode

import httpx
import pytest

from runway.mediastore import (
    IncompleteUpload,
    InvalidUploadRequest,
    ObjectNotFound,
    PartReceipt,
    R2Config,
    R2Store,
    SizeMismatch,
    StorageBackendError,
    UnsupportedOperation,
    UploadNotFound,
    make_media_key,
)
from runway.mediastore import r2 as r2_module
from runway.mediastore.sigv4 import presign_url, sha256_hex, sign_headers
from runway.platforms.limits import GiB, MiB

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
                return self._complete(params["uploadId"], upload, request.content)
            if method == "DELETE":
                del self.uploads[params["uploadId"]]
                return httpx.Response(204)
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
            if "range" not in request.headers:
                return httpx.Response(200, content=data)
            first, last = map(int, request.headers["range"][len("bytes=") :].split("-"))
            return httpx.Response(206, content=data[first : last + 1])
        if method == "DELETE":
            self.objects.pop(key, None)
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {method} {request.url}")

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


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def fake(clock: Clock) -> FakeR2:
    return FakeR2(clock)


@pytest.fixture()
def store(fake: FakeR2, clock: Clock) -> R2Store:
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    return R2Store(CONFIG, client=client, part_size=PART, clock=clock)


@pytest.fixture()
def browser(fake: FakeR2) -> httpx.Client:
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


def test_direct_upload_round_trip_without_bytes_through_the_api(
    store: R2Store, fake: FakeR2, browser: httpx.Client
) -> None:
    data = _payload(2 * PART + 123)
    key, digest = _key(data)
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest)
    assert (plan.store, plan.mode, plan.part_count, plan.part_size) == ("r2", "direct", 3, PART)

    urls = store.part_upload_urls(plan)
    assert sorted(urls) == [1, 2, 3]
    assert all(url.startswith(f"https://{HOST}/{BUCKET}/w/ws1/asset1/") for url in urls.values())
    assert all("content-length" in url for url in urls.values())
    _upload_parts(browser, urls, data)

    progress = store.upload_progress(plan)
    assert progress.is_complete and progress.bytes_received == len(data)
    store.complete_upload(plan)
    assert fake.objects[key] == data
    assert store.object_size(key) == len(data)

    # The API process never carried media bytes: every request it signed was small.
    assert max(len(request.content) for request in fake.signed_requests) < 10_000

    with store.open(key) as handle:
        handle.seek(PART - 3)
        assert handle.read(10) == data[PART - 3 : PART + 7]
        handle.seek(-5, 2)
        assert handle.read() == data[-5:]

    signed = store.signed_get_url(key, 7200)
    assert signed is not None and "X-Amz-Expires=7200" in signed
    assert browser.get(signed).content == data

    store.delete(key)
    store.delete(key)
    assert key not in fake.objects


def test_part_url_refuses_a_body_of_another_length(store: R2Store, browser: httpx.Client) -> None:
    data = _payload(PART + 10)
    key, digest = _key(data)
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest)
    url = store.part_upload_urls(plan, [2])[2]
    response = browser.put(url, content=data[PART:] + b"extra")
    assert response.status_code == 403
    assert store.upload_progress(plan).missing_part_numbers == (1, 2)


def test_resume_uploads_only_missing_parts_with_fresh_urls(
    store: R2Store, fake: FakeR2, browser: httpx.Client, clock: Clock
) -> None:
    data = _payload(2 * PART + 1)
    key, digest = _key(data)
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest)
    urls = store.part_upload_urls(plan, ttl_seconds=600)
    _upload_parts(browser, {1: urls[1]}, data)

    clock.now += timedelta(minutes=30)
    assert browser.put(urls[2], content=data[PART : 2 * PART]).status_code == 403  # expired
    progress = store.upload_progress(plan)
    assert progress.missing_part_numbers == (2, 3)
    before = len(fake.presigned_requests)
    _upload_parts(browser, store.part_upload_urls(plan, progress.missing_part_numbers), data)
    assert len(fake.presigned_requests) - before == 2
    store.complete_upload(plan)
    assert fake.objects[key] == data


def test_list_parts_follows_pagination(
    store: R2Store, browser: httpx.Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(r2_module, "_LIST_PAGE_SIZE", 2)
    data = _payload(4 * PART)
    key, digest = _key(data)
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest)
    _upload_parts(browser, store.part_upload_urls(plan), data)
    assert [part.part_number for part in store.upload_progress(plan).parts] == [1, 2, 3, 4]


def test_complete_refuses_missing_or_wrong_sized_parts(
    store: R2Store, fake: FakeR2, browser: httpx.Client
) -> None:
    data = _payload(2 * PART)
    key, digest = _key(data)
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest)
    urls = store.part_upload_urls(plan)
    _upload_parts(browser, {1: urls[1]}, data)
    with pytest.raises(IncompleteUpload, match="2"):
        store.complete_upload(plan)

    # A part that reached the bucket with the wrong size (for example, a store that did not
    # enforce the signed length) is caught before the object is assembled.
    fake.uploads[plan.upload_id]["parts"][2] = (b"short", hashlib.md5(b"short").hexdigest())
    with pytest.raises(SizeMismatch):
        store.complete_upload(plan)
    assert key not in fake.objects


def test_complete_checks_client_receipts(store: R2Store, browser: httpx.Client) -> None:
    data = _payload(PART)
    key, digest = _key(data)
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest)
    _upload_parts(browser, store.part_upload_urls(plan), data)
    with pytest.raises(InvalidUploadRequest):
        store.complete_upload(plan, [PartReceipt(1, PART, '"not-the-etag"')])
    good = store.upload_progress(plan).parts[0]
    store.complete_upload(plan, [PartReceipt(1, PART, f'"{good.etag}"')])


def test_complete_detects_an_error_in_a_200_body(
    store: R2Store, fake: FakeR2, browser: httpx.Client
) -> None:
    data = _payload(PART)
    key, digest = _key(data)
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest)
    _upload_parts(browser, store.part_upload_urls(plan), data)
    fake.complete_error_in_body = True
    with pytest.raises(StorageBackendError, match="InternalError") as raised:
        store.complete_upload(plan)
    assert raised.value.retryable


def test_complete_is_idempotent(store: R2Store, browser: httpx.Client) -> None:
    data = _payload(PART)
    key, digest = _key(data)
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest)
    _upload_parts(browser, store.part_upload_urls(plan), data)
    store.complete_upload(plan)
    store.complete_upload(plan)


def test_abort_then_progress_reports_upload_not_found(store: R2Store) -> None:
    data = _payload(10)
    key, digest = _key(data)
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest)
    store.abort_upload(plan)
    store.abort_upload(plan)
    with pytest.raises(UploadNotFound):
        store.upload_progress(plan)
    with pytest.raises(UploadNotFound):
        store.complete_upload(plan)


def test_abort_stale_uploads_lists_pages_and_keeps_recent_and_foreign_ones(
    store: R2Store, fake: FakeR2, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(r2_module, "_LIST_PAGE_SIZE", 1)
    old_plans = []
    for asset in ("a1", "a2"):
        key, digest = _key(asset.encode(), asset)
        old_plans.append(store.begin_upload(key, 10, "video/mp4", sha256=digest))
    fake.uploads["foreign"] = {"key": "w/not-a-media-key", "parts": {}, "initiated": START}
    clock.now = START + timedelta(days=3)
    recent_key, recent_digest = _key(b"recent", "a3")
    recent = store.begin_upload(recent_key, 10, "video/mp4", sha256=recent_digest)

    aborted = store.abort_stale_uploads(START + timedelta(days=1))
    assert sorted(item.upload_id for item in aborted) == sorted(p.upload_id for p in old_plans)
    assert aborted[0].initiated_at == START
    assert set(fake.uploads) == {"foreign", recent.upload_id}


def test_signed_get_url_is_capped_at_seven_days(store: R2Store) -> None:
    key, _ = _key(b"x")
    url = store.signed_get_url(key, 7 * 24 * 3600)
    assert url is not None and "X-Amz-Expires=604800" in url
    with pytest.raises(ValueError):
        store.signed_get_url(key, 7 * 24 * 3600 + 1)
    with pytest.raises(ValueError):
        store.signed_get_url(key, 0)


def test_large_files_get_larger_parts_within_r2_limits(store: R2Store) -> None:
    size = 256 * 10**9
    key, digest = _key(b"big")
    plan = store.begin_upload(key, size, "video/mp4", sha256=digest)
    assert plan.part_count <= 10_000
    assert plan.part_size % MiB == 0 and plan.part_size >= PART
    with pytest.raises(InvalidUploadRequest):
        store.begin_upload(key, 10_000 * 5 * GiB + 1, "video/mp4", sha256=digest)


def test_open_missing_object_and_changed_object(store: R2Store, fake: FakeR2) -> None:
    key, _ = _key(b"x")
    with pytest.raises(ObjectNotFound):
        store.open(key)
    fake.objects[key] = b"first version"
    handle = store.open(key)
    fake.objects[key] = b"other version"
    with pytest.raises(StorageBackendError, match="412"):
        handle.read()


def test_failures_become_storage_errors_without_secrets(fake: FakeR2, clock: Clock) -> None:
    key, digest = _key(b"x")

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    offline = R2Store(
        CONFIG, client=httpx.Client(transport=httpx.MockTransport(broken)), clock=clock
    )
    with pytest.raises(StorageBackendError) as raised:
        offline.begin_upload(key, 10, "video/mp4", sha256=digest)
    assert raised.value.retryable
    assert CONFIG.secret_access_key not in str(raised.value)

    fake.fail_with = 503
    failing = R2Store(
        CONFIG, client=httpx.Client(transport=httpx.MockTransport(fake.handler)), clock=clock
    )
    with pytest.raises(StorageBackendError) as raised:
        failing.delete(key)
    assert (raised.value.status, raised.value.retryable) == (503, True)


def test_the_fake_rejects_a_wrong_signature(fake: FakeR2, clock: Clock) -> None:
    wrong = R2Config(
        account_id=ACCOUNT, bucket=BUCKET, access_key_id="test-access-key", secret_access_key="x"
    )
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    key, digest = _key(b"x")
    with pytest.raises(StorageBackendError, match="SignatureDoesNotMatch"):
        R2Store(wrong, client=client, clock=clock).begin_upload(key, 10, "video/mp4", sha256=digest)


def test_r2_refuses_api_uploads_and_foreign_plans(store: R2Store, tmp_path: Any) -> None:
    from runway.mediastore import LocalDiskStore

    data = b"0123456789"
    key, digest = _key(data)
    local_plan = LocalDiskStore(tmp_path).begin_upload(key, 10, "video/mp4", sha256=digest)
    with pytest.raises(UploadNotFound):
        store.upload_progress(local_plan)
    plan = store.begin_upload(key, 10, "video/mp4", sha256=digest)
    with pytest.raises(UnsupportedOperation):
        store.receive_part(plan, 1, data)


@pytest.mark.parametrize(
    "change",
    [
        {"account_id": "bad/account"},
        {"bucket": "Bad_Bucket"},
        {"secret_access_key": ""},
        {"endpoint": "http://insecure.example"},
    ],
)
def test_r2_config_validation(change: dict[str, str]) -> None:
    values: dict[str, Any] = {
        "account_id": ACCOUNT,
        "bucket": BUCKET,
        "access_key_id": "id",
        "secret_access_key": "secret",
        **change,
    }
    with pytest.raises(ValueError):
        R2Config(**values)


def test_r2_config_repr_hides_the_secret() -> None:
    assert CONFIG.secret_access_key not in repr(CONFIG)
