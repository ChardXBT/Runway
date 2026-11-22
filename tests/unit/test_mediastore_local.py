from __future__ import annotations

import hashlib
import io
import os
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from runway.config import Settings
from runway.mediastore import (
    ChecksumMismatch,
    IncompleteUpload,
    InvalidMediaKey,
    InvalidUploadRequest,
    LocalDiskStore,
    ObjectExists,
    ObjectNotFound,
    PartReceipt,
    UnsupportedOperation,
    UploadNotFound,
    UploadPlan,
    make_media_key,
    parse_media_key,
    safe_extension,
    workspace_prefix,
)

PART = 1024
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def _payload(size: int) -> bytes:
    return bytes((index * 7 + index // 251) % 256 for index in range(size))


def _key(data: bytes, *, workspace: str = "ws1", asset: str = "asset1") -> tuple[str, str]:
    digest = hashlib.sha256(data).hexdigest()
    return make_media_key(workspace, asset, digest, "mp4"), digest


def _store(root: Path, **options: object) -> LocalDiskStore:
    return LocalDiskStore(root, part_size=PART, clock=lambda: NOW, **options)  # type: ignore[arg-type]


def _send_all(store: LocalDiskStore, plan: UploadPlan, data: bytes, order: list[int]) -> None:
    for number in order:
        offset = plan.part_offset(number)
        store.receive_part(plan, number, data[offset : offset + plan.part_length(number)])


# Keys ----------------------------------------------------------------------------------------


def test_media_key_format_and_round_trip() -> None:
    digest = "AB" * 32
    key = make_media_key(7, "a-1", digest, ".MP4")
    assert key == "w/7/a-1/abababababababab.mp4"
    parsed = parse_media_key(key)
    assert (parsed.workspace_id, parsed.asset_id, parsed.extension) == ("7", "a-1", "mp4")
    assert parsed.workspace_prefix == workspace_prefix(7) == "w/7/"


@pytest.mark.parametrize(
    ("filename", "extension"),
    [
        ("clip.MOV", "mov"),
        ("a/b\\c.webm", "webm"),
        ("noext", "bin"),
        ("x.toolongextension", "bin"),
        ("evil.mp4/../..", "bin"),
        (None, "bin"),
    ],
)
def test_safe_extension(filename: str | None, extension: str) -> None:
    assert safe_extension(filename) == extension


@pytest.mark.parametrize(
    ("workspace", "asset", "digest", "ext"),
    [
        ("..", "a", "a" * 64, "mp4"),
        ("w/1", "a", "a" * 64, "mp4"),
        ("con", "a", "a" * 64, "mp4"),
        ("1", "LPT1", "a" * 64, "mp4"),
        ("1", "a", "a" * 63, "mp4"),
        ("1", "a", "g" * 64, "mp4"),
        ("1", "a", "a" * 64, "m/p4"),
        ("1", "a", "a" * 64, ""),
    ],
)
def test_make_media_key_rejects_unsafe_components(
    workspace: str, asset: str, digest: str, ext: str
) -> None:
    with pytest.raises(InvalidMediaKey):
        make_media_key(workspace, asset, digest, ext)


_HASH = "0123456789abcdef"
_ESCAPING_KEYS = [
    "../outside.mp4",
    f"w/../../{_HASH}.mp4",
    f"w/1/../../../{_HASH}.mp4",
    f"w/1/2/../{_HASH}.mp4",
    f"w/./2/{_HASH}.mp4",
    f"/w/1/2/{_HASH}.mp4",
    f"w\\1\\2\\{_HASH}.mp4",
    f"C:\\w\\1\\2\\{_HASH}.mp4",
    f"\\\\server\\share\\w\\1\\2\\{_HASH}.mp4",
    f"w/1/2/{_HASH}.mp4:hidden",
    f"w/1/2/{_HASH}.mp4\n",
    f"w/1/2/{_HASH.upper()}.mp4",
    f"w/con/2/{_HASH}.mp4",
    f"w/1/nul/{_HASH}.mp4",
    f"w/1/2/{_HASH}.",
    f"w/1/2/{_HASH}",
    f"w/1/2/3/{_HASH}.mp4",
    f"w/1/2/{_HASH}.mp4/..",
    f"w/1/2/%2e%2e/{_HASH}.mp4",
    "",
]


@pytest.mark.parametrize("key", _ESCAPING_KEYS)
def test_every_operation_rejects_keys_outside_the_format(tmp_path: Path, key: str) -> None:
    store = _store(tmp_path / "media")
    with pytest.raises(InvalidMediaKey):
        store.begin_upload(key, 10, "video/mp4", sha256=_HASH + "0" * 48)
    with pytest.raises(InvalidMediaKey):
        store.open(key)
    with pytest.raises(InvalidMediaKey):
        store.delete(key)
    with pytest.raises(InvalidMediaKey):
        store.signed_get_url(key, 60)
    assert not any((tmp_path / "media").glob("**/*.mp4"))


def test_a_link_inside_the_root_cannot_lead_outside(tmp_path: Path) -> None:
    root = tmp_path / "media"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "w").mkdir(parents=True)
    link = root / "w" / "evil"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        # Symlinks need a privilege on Windows; a junction does not.
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True
        )
    data = b"x" * 10
    key, digest = _key(data, workspace="evil")
    store = _store(root)
    with pytest.raises(InvalidMediaKey):
        store.begin_upload(key, len(data), "video/mp4", sha256=digest)
    with pytest.raises(InvalidMediaKey):
        store.open(key)


# Uploads ---------------------------------------------------------------------------------


def test_parts_in_any_order_complete_into_the_key_path(tmp_path: Path) -> None:
    data = _payload(3 * PART + 100)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = store.begin_upload(key, len(data), "video/MP4; codecs=avc1", sha256=digest.upper())
    assert (plan.mode, plan.store, plan.part_count, plan.content_type) == (
        "api",
        "local",
        4,
        "video/mp4",
    )
    assert plan.sha256 == digest

    _send_all(store, plan, data, [4, 2])
    progress = store.upload_progress(plan)
    assert progress.missing_part_numbers == (1, 3)
    assert progress.received_ranges == ((PART, 2 * PART - 1), (3 * PART, 3 * PART + 99))
    assert progress.bytes_received == PART + 100
    assert not progress.is_complete

    _send_all(store, plan, data, [1, 3])
    progress = store.upload_progress(plan)
    assert progress.is_complete
    assert progress.received_ranges == ((0, len(data) - 1),)

    store.complete_upload(plan, progress.parts)
    final = tmp_path / "media" / Path(*key.split("/"))
    assert final.read_bytes() == data
    assert list((tmp_path / "media" / ".staging").iterdir()) == []

    with store.open(key) as handle:
        handle.seek(PART + 5)
        assert handle.read(10) == data[PART + 5 : PART + 15]
    assert store.signed_get_url(key, 7200) is None
    assert store.path_for(key) == final.resolve()


def test_path_for_requires_a_completed_object(tmp_path: Path) -> None:
    data = _payload(10)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    store.begin_upload(key, 10, "video/mp4", sha256=digest)
    with pytest.raises(ObjectNotFound):
        store.path_for(key)
    with pytest.raises(InvalidMediaKey):
        store.path_for("w/../../x.mp4")


def test_upload_resumes_after_restart_without_resending_parts(tmp_path: Path) -> None:
    data = _payload(2 * PART + 1)
    key, digest = _key(data)
    first = _store(tmp_path / "media")
    plan = first.begin_upload(key, len(data), "video/mp4", sha256=digest)
    first.receive_part(plan, 1, data[:PART])

    # A new process with the persisted plan sees what arrived and asks only for the rest.
    restored = UploadPlan.from_dict(plan.to_dict())
    second = _store(tmp_path / "media")
    progress = second.upload_progress(restored)
    assert [part.part_number for part in progress.parts] == [1]
    assert progress.missing_part_numbers == (2, 3)
    _send_all(second, restored, data, list(progress.missing_part_numbers))
    second.complete_upload(restored)
    with second.open(key) as handle:
        assert handle.read() == data


def test_part_from_a_stream_and_receipt_etag(tmp_path: Path) -> None:
    data = _payload(PART)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest)
    receipt = store.receive_part(plan, 1, io.BytesIO(data))
    assert receipt == PartReceipt(1, PART, hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize("length", [PART - 1, PART + 1, 0])
def test_part_with_the_wrong_length_is_rejected_and_not_recorded(
    tmp_path: Path, length: int
) -> None:
    data = _payload(2 * PART)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest)
    store.receive_part(plan, 1, data[:PART])
    with pytest.raises(InvalidUploadRequest):
        store.receive_part(plan, 1, io.BytesIO(_payload(length)))
    # The earlier good copy of part 1 was forgotten before the failed rewrite.
    assert store.upload_progress(plan).missing_part_numbers == (1, 2)


@pytest.mark.parametrize("number", [0, 3, -1])
def test_part_number_outside_the_plan_is_rejected(tmp_path: Path, number: int) -> None:
    data = _payload(2 * PART)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest)
    with pytest.raises(InvalidUploadRequest):
        store.receive_part(plan, number, b"x")


def test_same_part_cannot_be_received_twice_at_once(tmp_path: Path) -> None:
    data = _payload(PART)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest)
    started = threading.Event()
    release = threading.Event()

    class SlowStream(io.RawIOBase):
        def __init__(self) -> None:
            self._inner = io.BytesIO(data)

        def read(self, size: int = -1) -> bytes:
            started.set()
            release.wait(5)
            return self._inner.read(size)

    errors: list[BaseException] = []

    def first_sender() -> None:
        try:
            store.receive_part(plan, 1, SlowStream())  # type: ignore[arg-type]
        except BaseException as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)

    thread = threading.Thread(target=first_sender)
    thread.start()
    assert started.wait(5)
    with pytest.raises(InvalidUploadRequest, match="already being received"):
        store.receive_part(plan, 1, data)
    release.set()
    thread.join(5)
    assert errors == []
    assert store.upload_progress(plan).is_complete


def test_complete_requires_every_part(tmp_path: Path) -> None:
    data = _payload(3 * PART)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest)
    store.receive_part(plan, 2, data[PART : 2 * PART])
    with pytest.raises(IncompleteUpload, match="1, 3"):
        store.complete_upload(plan)


def test_complete_verifies_sha256_and_keeps_staging_until_abort(tmp_path: Path) -> None:
    data = _payload(PART)
    key, _ = _key(data)
    claimed = _HASH + "f" * 48  # Matches the key prefix but not the bytes.
    key = make_media_key("ws1", "asset1", claimed, "mp4")
    store = _store(tmp_path / "media")
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=claimed)
    store.receive_part(plan, 1, data)
    with pytest.raises(ChecksumMismatch):
        store.complete_upload(plan)
    with pytest.raises(ObjectNotFound):
        store.open(key)
    store.abort_upload(plan)
    assert list((tmp_path / "media" / ".staging").iterdir()) == []
    with pytest.raises(UploadNotFound):
        store.upload_progress(plan)


def test_complete_rejects_receipts_that_do_not_match(tmp_path: Path) -> None:
    data = _payload(PART)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest)
    store.receive_part(plan, 1, data)
    with pytest.raises(InvalidUploadRequest):
        store.complete_upload(plan, [PartReceipt(1, PART, "0" * 64)])


def test_complete_is_idempotent(tmp_path: Path) -> None:
    data = _payload(PART)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest)
    store.receive_part(plan, 1, data)
    store.complete_upload(plan)
    store.complete_upload(plan)
    with pytest.raises(ObjectExists):
        store.begin_upload(key, len(data), "video/mp4", sha256=digest)


def test_begin_rejects_bad_requests(tmp_path: Path) -> None:
    data = _payload(10)
    key, digest = _key(data)
    store = _store(tmp_path / "media", max_object_bytes=100)
    with pytest.raises(InvalidUploadRequest, match="limit"):
        store.begin_upload(key, 101, "video/mp4", sha256=digest)
    with pytest.raises(InvalidUploadRequest):
        store.begin_upload(key, 0, "video/mp4", sha256=digest)
    with pytest.raises(InvalidUploadRequest):
        store.begin_upload(key, 10, "video/mp4\r\nX-Evil: 1", sha256=digest)
    with pytest.raises(InvalidUploadRequest, match="hash prefix"):
        store.begin_upload(key, 10, "video/mp4", sha256="f" * 64)


def test_a_plan_that_does_not_match_the_upload_is_refused(tmp_path: Path) -> None:
    data = _payload(PART)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = store.begin_upload(key, len(data), "video/mp4", sha256=digest)
    tampered = UploadPlan.from_dict({**plan.to_dict(), "size": 2 * PART})
    with pytest.raises(UploadNotFound):
        store.receive_part(tampered, 2, data)
    forged = UploadPlan.from_dict({**plan.to_dict(), "upload_id": "../../x"})
    with pytest.raises(UploadNotFound):
        store.upload_progress(forged)


def test_abort_stale_uploads_removes_only_old_ones(tmp_path: Path) -> None:
    root = tmp_path / "media"
    times = iter([NOW - timedelta(days=3), NOW])
    store = LocalDiskStore(root, part_size=PART, clock=lambda: next(times))
    old_data, new_data = _payload(10), _payload(11)
    old_key, old_digest = _key(old_data, asset="old")
    new_key, new_digest = _key(new_data, asset="new")
    old = store.begin_upload(old_key, 10, "video/mp4", sha256=old_digest)
    new = store.begin_upload(new_key, 11, "video/mp4", sha256=new_digest)
    aborted = store.abort_stale_uploads(NOW - timedelta(days=1))
    assert [(item.key, item.upload_id) for item in aborted] == [(old_key, old.upload_id)]
    with pytest.raises(UploadNotFound):
        store.upload_progress(old)
    assert store.upload_progress(new).missing_part_numbers == (1,)


def test_delete_is_idempotent_and_prunes_empty_directories(tmp_path: Path) -> None:
    data = _payload(10)
    key, digest = _key(data)
    root = tmp_path / "media"
    store = _store(root)
    plan = store.begin_upload(key, 10, "video/mp4", sha256=digest)
    store.receive_part(plan, 1, data)
    store.complete_upload(plan)
    store.delete(key)
    store.delete(key)
    assert not (root / "w").exists()
    with pytest.raises(ObjectNotFound):
        store.open(key)


def test_local_store_refuses_part_urls(tmp_path: Path) -> None:
    data = _payload(10)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = store.begin_upload(key, 10, "video/mp4", sha256=digest)
    with pytest.raises(UnsupportedOperation):
        store.part_upload_urls(plan)


def test_default_root_is_under_the_data_dir(tmp_path: Path) -> None:
    store = LocalDiskStore.from_settings(Settings(data_dir=tmp_path / "data"))
    assert store.root == (tmp_path / "data" / "media" / "uploads").resolve()
