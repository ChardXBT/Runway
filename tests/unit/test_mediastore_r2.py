"""R2Store against the in-memory S3 fake in ``tests/r2_support.py``. No network.

The fake authenticates every request the way S3 does: it recomputes the SigV4 signature of
what httpx actually sent, so a URL or header the client mangles after signing fails here.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from r2_support import (
    ACCOUNT,
    BUCKET,
    CONFIG,
    HOST,
    PART,
    START,
    WS,
    Clock,
    FakeR2,
    _key,
    _payload,
    _upload_parts,
    fake_r2_browser,
    fake_r2_store,
)

from runway.mediastore import (
    ChecksumMismatch,
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
    WorkspaceMismatch,
    make_media_key,
)
from runway.mediastore import r2 as r2_module
from runway.platforms.limits import GiB, MiB


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def fake(clock: Clock) -> FakeR2:
    return FakeR2(clock)


@pytest.fixture()
def store(fake: FakeR2, clock: Clock) -> R2Store:
    return fake_r2_store(fake, clock)


@pytest.fixture()
def browser(fake: FakeR2) -> httpx.Client:
    """Stands in for the user's browser: no credentials, only the URLs it was given."""
    return fake_r2_browser(fake)


def test_direct_upload_round_trip_without_bytes_through_the_api(
    store: R2Store, fake: FakeR2, browser: httpx.Client
) -> None:
    data = _payload(2 * PART + 123)
    key, digest = _key(data)
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest, workspace_id=WS)
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
    assert store.object_size(key, workspace_id=WS) == len(data)

    # The API process never carried media bytes: every request it signed was small.
    assert max(len(request.content) for request in fake.signed_requests) < 10_000

    with store.open(key, workspace_id=WS) as handle:
        handle.seek(PART - 3)
        assert handle.read(10) == data[PART - 3 : PART + 7]
        handle.seek(-5, 2)
        assert handle.read() == data[-5:]

    signed = store.signed_get_url(key, 7200, workspace_id=WS)
    assert signed is not None and "X-Amz-Expires=7200" in signed
    assert browser.get(signed).content == data

    store.delete(key, workspace_id=WS)
    store.delete(key, workspace_id=WS)
    assert key not in fake.objects


def test_part_url_refuses_a_body_of_another_length(store: R2Store, browser: httpx.Client) -> None:
    data = _payload(PART + 10)
    key, digest = _key(data)
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest, workspace_id=WS)
    url = store.part_upload_urls(plan, [2])[2]
    response = browser.put(url, content=data[PART:] + b"extra")
    assert response.status_code == 403
    assert store.upload_progress(plan).missing_part_numbers == (1, 2)


def test_resume_uploads_only_missing_parts_with_fresh_urls(
    store: R2Store, fake: FakeR2, browser: httpx.Client, clock: Clock
) -> None:
    data = _payload(2 * PART + 1)
    key, digest = _key(data)
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest, workspace_id=WS)
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
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest, workspace_id=WS)
    _upload_parts(browser, store.part_upload_urls(plan), data)
    assert [part.part_number for part in store.upload_progress(plan).parts] == [1, 2, 3, 4]


def test_complete_refuses_missing_or_wrong_sized_parts(
    store: R2Store, fake: FakeR2, browser: httpx.Client
) -> None:
    data = _payload(2 * PART)
    key, digest = _key(data)
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest, workspace_id=WS)
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
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest, workspace_id=WS)
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
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest, workspace_id=WS)
    _upload_parts(browser, store.part_upload_urls(plan), data)
    fake.complete_error_in_body = True
    with pytest.raises(StorageBackendError, match="InternalError") as raised:
        store.complete_upload(plan)
    assert raised.value.retryable


def test_complete_is_idempotent(store: R2Store, browser: httpx.Client) -> None:
    data = _payload(PART)
    key, digest = _key(data)
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest, workspace_id=WS)
    _upload_parts(browser, store.part_upload_urls(plan), data)
    store.complete_upload(plan)
    store.complete_upload(plan)


def test_abort_then_progress_reports_upload_not_found(store: R2Store) -> None:
    data = _payload(10)
    key, digest = _key(data)
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest, workspace_id=WS)
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
        old_plans.append(store.begin_upload(key, 10, "video/mp4", sha256=digest, workspace_id=WS))
    fake.uploads["foreign"] = {"key": "w/not-a-media-key", "parts": {}, "initiated": START}
    clock.now = START + timedelta(days=3)
    recent_key, recent_digest = _key(b"recent", "a3")
    recent = store.begin_upload(recent_key, 10, "video/mp4", sha256=recent_digest, workspace_id=WS)

    aborted = store.abort_stale_uploads(START + timedelta(days=1))
    assert sorted(item.upload_id for item in aborted) == sorted(p.upload_id for p in old_plans)
    assert aborted[0].initiated_at == START
    assert set(fake.uploads) == {"foreign", recent.upload_id}


def test_signed_get_url_is_capped_at_seven_days(store: R2Store) -> None:
    key, _ = _key(b"x")
    url = store.signed_get_url(key, 7 * 24 * 3600, workspace_id=WS)
    assert url is not None and "X-Amz-Expires=604800" in url
    with pytest.raises(ValueError):
        store.signed_get_url(key, 7 * 24 * 3600 + 1, workspace_id=WS)
    with pytest.raises(ValueError):
        store.signed_get_url(key, 0, workspace_id=WS)


def test_large_files_get_larger_parts_within_r2_limits(store: R2Store) -> None:
    size = 256 * 10**9
    key, digest = _key(b"big")
    plan = store.begin_upload(key, size, "video/mp4", sha256=digest, workspace_id=WS)
    assert plan.part_count <= 10_000
    assert plan.part_size % MiB == 0 and plan.part_size >= PART
    with pytest.raises(InvalidUploadRequest):
        store.begin_upload(key, 10_000 * 5 * GiB + 1, "video/mp4", sha256=digest, workspace_id=WS)


def test_open_missing_object_and_changed_object(store: R2Store, fake: FakeR2) -> None:
    key, _ = _key(b"x")
    with pytest.raises(ObjectNotFound):
        store.open(key, workspace_id=WS)
    fake.objects[key] = b"first version"
    handle = store.open(key, workspace_id=WS)
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
        offline.begin_upload(key, 10, "video/mp4", sha256=digest, workspace_id=WS)
    assert raised.value.retryable
    assert CONFIG.secret_access_key not in str(raised.value)

    fake.fail_with = 503
    failing = R2Store(
        CONFIG, client=httpx.Client(transport=httpx.MockTransport(fake.handler)), clock=clock
    )
    with pytest.raises(StorageBackendError) as raised:
        failing.delete(key, workspace_id=WS)
    assert (raised.value.status, raised.value.retryable) == (503, True)


def test_the_fake_rejects_a_wrong_signature(fake: FakeR2, clock: Clock) -> None:
    wrong = R2Config(
        account_id=ACCOUNT, bucket=BUCKET, access_key_id="test-access-key", secret_access_key="x"
    )
    client = httpx.Client(transport=httpx.MockTransport(fake.handler))
    key, digest = _key(b"x")
    with pytest.raises(StorageBackendError, match="SignatureDoesNotMatch"):
        R2Store(wrong, client=client, clock=clock).begin_upload(
            key, 10, "video/mp4", sha256=digest, workspace_id=WS
        )


def test_r2_refuses_api_uploads_and_foreign_plans(store: R2Store, tmp_path: Any) -> None:
    from runway.mediastore import LocalDiskStore

    data = b"0123456789"
    key, digest = _key(data)
    local_plan = LocalDiskStore(tmp_path).begin_upload(
        key, 10, "video/mp4", sha256=digest, workspace_id=WS
    )
    with pytest.raises(UploadNotFound):
        store.upload_progress(local_plan)
    plan = store.begin_upload(key, 10, "video/mp4", sha256=digest, workspace_id=WS)
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


# Review fixes ------------------------------------------------------------------------------


def _complete_object(store: R2Store, browser: httpx.Client, data: bytes) -> tuple[str, str]:
    key, digest = _key(data)
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest, workspace_id=WS)
    _upload_parts(browser, store.part_upload_urls(plan), data)
    store.complete_upload(plan)
    return key, digest


def test_copy_to_path_verifies_sha256_of_the_stored_bytes(
    store: R2Store, browser: httpx.Client, tmp_path: Path
) -> None:
    data = _payload(PART + 77)
    key, digest = _complete_object(store, browser, data)
    destination = tmp_path / "probe.input"
    store.copy_to_path(key, destination, workspace_id=WS, sha256=digest, size=len(data))
    assert destination.read_bytes() == data


def test_copy_to_path_rejects_bytes_that_do_not_match_the_declared_hash(
    store: R2Store, fake: FakeR2, browser: httpx.Client, tmp_path: Path
) -> None:
    # The client declares one hash (and key prefix) and uploads other bytes of the same size.
    declared = _payload(PART)
    key, digest = _key(declared)
    plan = store.begin_upload(key, PART, "video/mp4", sha256=digest, workspace_id=WS)
    _upload_parts(browser, store.part_upload_urls(plan), b"\x00" * PART)
    store.complete_upload(plan)  # R2 cannot check SHA-256 at completion.
    destination = tmp_path / "probe.input"
    with pytest.raises(ChecksumMismatch):
        store.copy_to_path(key, destination, workspace_id=WS, sha256=digest, size=PART)
    assert list(tmp_path.iterdir()) == []

    fake.objects[key] = declared + b"extra"
    with pytest.raises(SizeMismatch):
        store.copy_to_path(key, destination, workspace_id=WS, sha256=digest, size=PART)
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ObjectNotFound):
        store.copy_to_path(
            make_media_key(WS, "missing", digest, "mp4"),
            destination,
            workspace_id=WS,
            sha256=digest,
            size=PART,
        )


def test_keys_are_bound_to_the_callers_workspace(
    store: R2Store, fake: FakeR2, browser: httpx.Client, tmp_path: Path
) -> None:
    data = _payload(10)
    key, digest = _complete_object(store, browser, data)
    before = len(fake.signed_requests)
    for action in (
        lambda: store.open(key, workspace_id="ws2"),
        lambda: store.delete(key, workspace_id="ws2"),
        lambda: store.object_size(key, workspace_id="ws2"),
        lambda: store.signed_get_url(key, 60, workspace_id="ws2"),
        lambda: store.signed_preview_url(key, 60, workspace_id="ws2", content_type="video/mp4"),
        lambda: store.copy_to_path(
            key, tmp_path / "x.input", workspace_id="ws2", sha256=digest, size=10
        ),
        lambda: store.begin_upload(key, 10, "video/mp4", sha256=digest, workspace_id="ws2"),
    ):
        with pytest.raises(WorkspaceMismatch):
            action()
    assert len(fake.signed_requests) == before  # Refused before any request.
    assert key in fake.objects


@pytest.mark.parametrize(
    "content_type", ["text/html", "application/octet-stream", "image/svg+xml", "x/y"]
)
def test_begin_accepts_only_video_and_image_content_types(
    store: R2Store, fake: FakeR2, content_type: str
) -> None:
    key, digest = _key(b"x")
    with pytest.raises(InvalidUploadRequest):
        store.begin_upload(key, 10, content_type, sha256=digest, workspace_id=WS)
    assert fake.uploads == {}


def test_preview_url_forces_probed_type_and_attachment_but_fetch_url_stays_plain(
    store: R2Store, browser: httpx.Client
) -> None:
    data = _payload(100)
    key, _ = _complete_object(store, browser, data)
    preview = store.signed_preview_url(key, 600, workspace_id=WS, content_type="video/mp4")
    assert preview is not None
    response = browser.get(preview)
    assert response.status_code == 200 and response.content == data
    assert response.headers["content-type"] == "video/mp4"
    assert response.headers["content-disposition"] == "attachment"

    fetch = store.signed_get_url(key, 7200, workspace_id=WS)
    assert fetch is not None and "response-" not in fetch
    with pytest.raises(InvalidUploadRequest):
        store.signed_preview_url(key, 600, workspace_id=WS, content_type="text/html")


def test_ranged_read_stops_at_the_requested_length_when_range_is_ignored(
    store: R2Store, fake: FakeR2, browser: httpx.Client
) -> None:
    data = _payload(4 * MiB)
    key, _ = _complete_object(store, browser, data)
    fake.ignore_range = True
    fake.blocks_sent = 0
    assert store._read_range(key, 0, 99, None) == data[:100]
    assert fake.blocks_sent == 1  # One 64 KiB block, not the whole 4 MiB object.
    with pytest.raises(StorageBackendError, match="ignored the Range"):
        store._read_range(key, 1000, 1099, None)
    assert fake.blocks_sent <= 2


def test_completion_finished_in_parallel_returns_normally(
    store: R2Store, fake: FakeR2, browser: httpx.Client
) -> None:
    data = _payload(PART + 1)
    key, digest = _key(data)
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest, workspace_id=WS)
    _upload_parts(browser, store.part_upload_urls(plan), data)

    def other_worker_completes(upload_id: str) -> None:
        upload = fake.uploads.pop(upload_id)
        fake.objects[upload["key"]] = b"".join(
            upload["parts"][n][0] for n in sorted(upload["parts"])
        )

    fake.before_complete = other_worker_completes
    store.complete_upload(plan)
    assert fake.objects[key] == data


def test_completion_404_without_an_object_is_upload_not_found(
    store: R2Store, fake: FakeR2, browser: httpx.Client
) -> None:
    data = _payload(PART)
    key, digest = _key(data)
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest, workspace_id=WS)
    _upload_parts(browser, store.part_upload_urls(plan), data)
    fake.before_complete = lambda upload_id: fake.uploads.pop(upload_id)
    with pytest.raises(UploadNotFound):
        store.complete_upload(plan)
