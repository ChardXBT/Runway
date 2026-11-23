from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

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
    SizeMismatch,
    UnsupportedOperation,
    UploadNotFound,
    UploadPlan,
    WorkspaceMismatch,
    check_workspace,
    key_extension,
    make_media_key,
    parse_media_key,
    workspace_prefix,
)
from runway.mediastore import local as local_module
from runway.mediastore import store as store_module

PART = 1024
NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
WS = "ws1"


def _payload(size: int) -> bytes:
    return bytes((index * 7 + index // 251) % 256 for index in range(size))


def _key(data: bytes, *, workspace: str = WS, asset: str = "asset1") -> tuple[str, str]:
    digest = hashlib.sha256(data).hexdigest()
    return make_media_key(workspace, asset, digest, "mp4"), digest


def _store(root: Path, **options: Any) -> LocalDiskStore:
    return LocalDiskStore(root, part_size=PART, clock=lambda: NOW, **options)


def _begin(store: LocalDiskStore, key: str, size: int, digest: str, **kw: Any) -> UploadPlan:
    return store.begin_upload(key, size, "video/mp4", sha256=digest, workspace_id=WS, **kw)


def _send_all(store: LocalDiskStore, plan: UploadPlan, data: bytes, order: list[int]) -> None:
    for number in order:
        offset = plan.part_offset(number)
        store.receive_part(plan, number, data[offset : offset + plan.part_length(number)])


def _completed(store: LocalDiskStore, data: bytes, asset: str = "asset1") -> str:
    key, digest = _key(data, asset=asset)
    plan = _begin(store, key, len(data), digest)
    _send_all(store, plan, data, list(plan.part_numbers()))
    store.complete_upload(plan)
    return key


# Keys ----------------------------------------------------------------------------------------


def test_media_key_format_and_round_trip() -> None:
    digest = "AB" * 32
    key = make_media_key(7, "a-1", digest, "mp4")
    assert key == "w/7/a-1/abababababababab.mp4"
    parsed = parse_media_key(key)
    assert (parsed.workspace_id, parsed.asset_id, parsed.extension) == ("7", "a-1", "mp4")
    assert parsed.workspace_prefix == workspace_prefix(7) == "w/7/"


@pytest.mark.parametrize(
    ("content_type", "extension"),
    [
        ("video/mp4", "mp4"),
        ("VIDEO/QuickTime", "mov"),
        ("video/webm", "webm"),
        ("image/jpeg", "jpg"),
        ("application/vnd.apple.mpegurl", "bin"),
        ("video/x-matroska", "bin"),
    ],
)
def test_key_extension_is_chosen_by_the_server(content_type: str, extension: str) -> None:
    assert key_extension(content_type) == extension


@pytest.mark.parametrize(
    ("workspace", "asset", "digest", "ext"),
    [
        ("..", "a", "a" * 64, "mp4"),
        ("w/1", "a", "a" * 64, "mp4"),
        ("con", "a", "a" * 64, "mp4"),
        ("1", "lpt1", "a" * 64, "mp4"),
        ("1", "a", "a" * 63, "mp4"),
        ("1", "a", "g" * 64, "mp4"),
        ("1", "a", "a" * 64, "m/p4"),
        ("1", "a", "a" * 64, ""),
        ("1", "a", "a" * 64, "m3u8"),  # Not an extension the server issues.
        ("1", "a", "a" * 64, "MP4"),
        ("Ws", "a", "a" * 64, "mp4"),  # Uppercase would alias w/ws/ on Windows.
        ("1", "Asset", "a" * 64, "mp4"),
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
    f"w/1/2/{_HASH}.m3u8",
    f"w/WS1/2/{_HASH}.mp4",
    "",
]


@pytest.mark.parametrize("key", _ESCAPING_KEYS)
def test_every_operation_rejects_keys_outside_the_format(tmp_path: Path, key: str) -> None:
    store = _store(tmp_path / "media")
    with pytest.raises(InvalidMediaKey):
        store.begin_upload(key, 10, "video/mp4", sha256=_HASH + "0" * 48, workspace_id="1")
    with pytest.raises(InvalidMediaKey):
        store.open(key, workspace_id="1")
    with pytest.raises(InvalidMediaKey):
        store.delete(key, workspace_id="1")
    with pytest.raises(InvalidMediaKey):
        store.signed_get_url(key, 60, workspace_id="1")
    assert not any((tmp_path / "media").glob("**/*.mp4"))


def test_keys_are_bound_to_the_callers_workspace(tmp_path: Path) -> None:
    store = _store(tmp_path / "media")
    data = _payload(10)
    key = _completed(store, data)
    other = "ws2"
    assert check_workspace(key, WS).workspace_id == WS
    for action in (
        lambda: store.open(key, workspace_id=other),
        lambda: store.delete(key, workspace_id=other),
        lambda: store.signed_get_url(key, 60, workspace_id=other),
        lambda: store.signed_preview_url(key, 60, workspace_id=other, content_type="video/mp4"),
        lambda: store.copy_to_path(
            key,
            tmp_path / "x.input",
            workspace_id=other,
            sha256=hashlib.sha256(data).hexdigest(),
            size=10,
        ),
        lambda: store.begin_upload(
            key, 10, "video/mp4", sha256=hashlib.sha256(data).hexdigest(), workspace_id=other
        ),
    ):
        with pytest.raises(WorkspaceMismatch):
            action()
    # The object is untouched and still readable by its own workspace.
    with store.open(key, workspace_id=WS) as handle:
        assert handle.read() == data


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
        store.begin_upload(key, len(data), "video/mp4", sha256=digest, workspace_id="evil")
    with pytest.raises(InvalidMediaKey):
        store.open(key, workspace_id="evil")


# Uploads ---------------------------------------------------------------------------------


def test_parts_in_any_order_complete_into_the_key_path(tmp_path: Path) -> None:
    data = _payload(3 * PART + 100)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = store.begin_upload(
        key, len(data), "video/MP4; codecs=avc1", sha256=digest.upper(), workspace_id=WS
    )
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

    with store.open(key, workspace_id=WS) as handle:
        handle.seek(PART + 5)
        assert handle.read(10) == data[PART + 5 : PART + 15]
    assert store.signed_get_url(key, 7200, workspace_id=WS) is None
    assert store.signed_preview_url(key, 600, workspace_id=WS, content_type="video/mp4") is None


def test_copy_to_path_links_and_verifies(tmp_path: Path) -> None:
    store = _store(tmp_path / "media")
    data = _payload(3000)
    key = _completed(store, data)
    digest = hashlib.sha256(data).hexdigest()
    destination = store.scratch_dir / "probe.input"
    store.copy_to_path(key, destination, workspace_id=WS, sha256=digest, size=len(data))
    assert destination.read_bytes() == data
    with pytest.raises(SizeMismatch):
        store.copy_to_path(key, tmp_path / "a.input", workspace_id=WS, sha256=digest, size=1)
    with pytest.raises(ChecksumMismatch):
        store.copy_to_path(
            key, tmp_path / "b.input", workspace_id=WS, sha256="0" * 64, size=len(data)
        )
    assert not (tmp_path / "a.input").exists() and not (tmp_path / "b.input").exists()
    assert [p.name for p in tmp_path.iterdir() if p.suffix == ".tmp"] == []
    with pytest.raises(ObjectNotFound):
        store.copy_to_path(
            make_media_key(WS, "gone", digest, "mp4"),
            tmp_path / "c.input",
            workspace_id=WS,
            sha256=digest,
            size=len(data),
        )


def test_copy_to_path_falls_back_to_a_verified_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _store(tmp_path / "media")
    data = _payload(2000)
    key = _completed(store, data)

    def no_links(source: Any, target: Any) -> None:
        raise OSError("cross-device link")

    monkeypatch.setattr(local_module.os, "link", no_links)
    destination = tmp_path / "other-volume" / "probe.input"
    store.copy_to_path(
        key, destination, workspace_id=WS, sha256=hashlib.sha256(data).hexdigest(), size=2000
    )
    assert destination.read_bytes() == data


def test_upload_resumes_after_restart_without_resending_parts(tmp_path: Path) -> None:
    data = _payload(2 * PART + 1)
    key, digest = _key(data)
    first = _store(tmp_path / "media")
    plan = _begin(first, key, len(data), digest)
    first.receive_part(plan, 1, data[:PART])

    # A new process with the persisted plan sees what arrived and asks only for the rest.
    restored = UploadPlan.from_dict(plan.to_dict())
    second = _store(tmp_path / "media")
    progress = second.upload_progress(restored)
    assert [part.part_number for part in progress.parts] == [1]
    assert progress.missing_part_numbers == (2, 3)
    _send_all(second, restored, data, list(progress.missing_part_numbers))
    second.complete_upload(restored)
    with second.open(key, workspace_id=WS) as handle:
        assert handle.read() == data


def test_part_from_a_stream_and_receipt_etag(tmp_path: Path) -> None:
    data = _payload(PART)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = _begin(store, key, len(data), digest)
    receipt = store.receive_part(plan, 1, io.BytesIO(data))
    assert receipt == PartReceipt(1, PART, hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize("length", [PART - 1, PART + 1, 0])
def test_part_with_the_wrong_length_is_rejected_and_not_recorded(
    tmp_path: Path, length: int
) -> None:
    data = _payload(2 * PART)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = _begin(store, key, len(data), digest)
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
    plan = _begin(store, key, len(data), digest)
    with pytest.raises(InvalidUploadRequest):
        store.receive_part(plan, number, b"x")


class _SlowStream(io.RawIOBase):
    def __init__(self, data: bytes, started: threading.Event, release: threading.Event):
        self._inner = io.BytesIO(data)
        self._started = started
        self._release = release

    def read(self, size: int = -1) -> bytes:
        self._started.set()
        self._release.wait(5)
        return self._inner.read(size)


@pytest.mark.parametrize("second_instance", [False, True])
def test_same_part_cannot_be_received_twice_at_once(tmp_path: Path, second_instance: bool) -> None:
    data = _payload(PART)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    other = _store(tmp_path / "media") if second_instance else store
    plan = _begin(store, key, len(data), digest)
    started, release = threading.Event(), threading.Event()
    errors: list[BaseException] = []

    def first_sender() -> None:
        try:
            store.receive_part(plan, 1, _SlowStream(data, started, release))  # type: ignore[arg-type]
        except BaseException as exc:  # pragma: no cover - surfaced by the assert below
            errors.append(exc)

    thread = threading.Thread(target=first_sender)
    thread.start()
    assert started.wait(5)
    with pytest.raises(InvalidUploadRequest, match="already being received"):
        other.receive_part(plan, 1, data)
    release.set()
    thread.join(5)
    assert errors == []
    assert other.upload_progress(plan).is_complete


_HOLD_LOCK = """
import sys
from pathlib import Path
from runway.mediastore.filelock import file_lock
with file_lock(Path(sys.argv[1])):
    print("locked", flush=True)
    sys.stdin.read()
"""


def test_a_part_held_by_another_process_is_refused(tmp_path: Path) -> None:
    data = _payload(PART)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = _begin(store, key, len(data), digest)
    lock_path = store.root / ".staging" / f"{plan.upload_id}.p1.lock"
    holder = subprocess.Popen(
        [sys.executable, "-c", _HOLD_LOCK, str(lock_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        with pytest.raises(InvalidUploadRequest, match="already being received"):
            store.receive_part(plan, 1, data)
    finally:
        holder.communicate("", timeout=10)
    store.receive_part(plan, 1, data)
    assert store.upload_progress(plan).is_complete


def test_parallel_parts_from_separate_instances_are_all_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    parts = 6
    data = _payload(parts * PART)
    key, digest = _key(data)
    root = tmp_path / "media"
    plan = _begin(_store(root), key, len(data), digest)
    original_load = LocalDiskStore._load_parts

    def slow_load(self: LocalDiskStore, plan: UploadPlan) -> dict[int, PartReceipt]:
        loaded = original_load(self, plan)
        time.sleep(0.02)  # Widen the read-modify-write window.
        return loaded

    monkeypatch.setattr(LocalDiskStore, "_load_parts", slow_load)
    threads = [
        threading.Thread(
            target=lambda n=n: _store(root).receive_part(plan, n, data[(n - 1) * PART : n * PART])
        )
        for n in range(1, parts + 1)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert _store(root).upload_progress(plan).is_complete


def test_state_replace_retries_a_transient_permission_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _payload(PART)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = _begin(store, key, len(data), digest)
    real_replace = os.replace
    failures = {"left": 2}

    def flaky_replace(source: Any, target: Any) -> None:
        if failures["left"]:
            failures["left"] -= 1
            raise PermissionError(13, "file in use")
        real_replace(source, target)

    monkeypatch.setattr(store_module.os, "replace", flaky_replace)
    monkeypatch.setattr(store_module, "_REPLACE_BACKOFF_SECONDS", 0.001)
    store.receive_part(plan, 1, data)
    assert failures["left"] == 0
    store.complete_upload(plan)

    def always_locked(source: Any, target: Any) -> None:
        raise PermissionError(13, "file in use")

    monkeypatch.setattr(store_module.os, "replace", always_locked)
    other, other_digest = _key(b"z" * 10, asset="other")
    with pytest.raises(PermissionError):
        _begin(store, other, 10, other_digest)
    leftovers = [p.name for p in (store.root / ".staging").iterdir() if p.suffix == ".tmp"]
    assert leftovers == []


def test_complete_requires_every_part(tmp_path: Path) -> None:
    data = _payload(3 * PART)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = _begin(store, key, len(data), digest)
    store.receive_part(plan, 2, data[PART : 2 * PART])
    with pytest.raises(IncompleteUpload, match="1, 3"):
        store.complete_upload(plan)


def test_complete_verifies_sha256_and_keeps_staging_until_abort(tmp_path: Path) -> None:
    data = _payload(PART)
    claimed = _HASH + "f" * 48  # Matches the key prefix but not the bytes.
    key = make_media_key(WS, "asset1", claimed, "mp4")
    store = _store(tmp_path / "media")
    plan = _begin(store, key, len(data), claimed)
    store.receive_part(plan, 1, data)
    with pytest.raises(ChecksumMismatch):
        store.complete_upload(plan)
    with pytest.raises(ObjectNotFound):
        store.open(key, workspace_id=WS)
    store.abort_upload(plan)
    assert list((tmp_path / "media" / ".staging").iterdir()) == []
    with pytest.raises(UploadNotFound):
        store.upload_progress(plan)


def test_complete_rejects_receipts_that_do_not_match(tmp_path: Path) -> None:
    data = _payload(PART)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = _begin(store, key, len(data), digest)
    store.receive_part(plan, 1, data)
    with pytest.raises(InvalidUploadRequest):
        store.complete_upload(plan, [PartReceipt(1, PART, "0" * 64)])


def test_complete_is_idempotent(tmp_path: Path) -> None:
    data = _payload(PART)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = _begin(store, key, len(data), digest)
    store.receive_part(plan, 1, data)
    store.complete_upload(plan)
    store.complete_upload(plan)
    with pytest.raises(ObjectExists):
        _begin(store, key, len(data), digest)


@pytest.mark.parametrize(
    "content_type",
    [
        "video/mp4\r\nX-Evil: 1",
        "text/html",
        "application/octet-stream",
        "application/vnd.apple.mpegurl",
        "image/svg+xml",
        "nonsense",
    ],
)
def test_begin_accepts_only_video_and_image_content_types(
    tmp_path: Path, content_type: str
) -> None:
    data = _payload(10)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    with pytest.raises(InvalidUploadRequest):
        store.begin_upload(key, 10, content_type, sha256=digest, workspace_id=WS)


def test_begin_rejects_bad_requests(tmp_path: Path) -> None:
    data = _payload(10)
    key, digest = _key(data)
    store = _store(tmp_path / "media", max_object_bytes=100)
    with pytest.raises(InvalidUploadRequest, match="limit"):
        _begin(store, key, 101, digest)
    with pytest.raises(InvalidUploadRequest):
        _begin(store, key, 0, digest)
    with pytest.raises(InvalidUploadRequest, match="hash prefix"):
        _begin(store, key, 10, "f" * 64)
    assert _begin(store, key, 10, digest).content_type == "video/mp4"
    image_key = make_media_key(WS, "img", digest, "jpg")
    plan = store.begin_upload(image_key, 10, "Image/JPEG", sha256=digest, workspace_id=WS)
    assert plan.content_type == "image/jpeg"


def test_a_plan_that_does_not_match_the_upload_is_refused(tmp_path: Path) -> None:
    data = _payload(PART)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = _begin(store, key, len(data), digest)
    tampered = UploadPlan.from_dict({**plan.to_dict(), "size": 2 * PART})
    with pytest.raises(UploadNotFound):
        store.receive_part(tampered, 2, data)
    forged = UploadPlan.from_dict({**plan.to_dict(), "upload_id": "../../x"})
    with pytest.raises(UploadNotFound):
        store.upload_progress(forged)


class _Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def test_stale_cleanup_uses_last_activity_not_start_time(tmp_path: Path) -> None:
    clock = _Clock(NOW - timedelta(days=3))
    store = LocalDiskStore(tmp_path / "media", part_size=PART, clock=clock)
    active_data, idle_data = _payload(2 * PART), _payload(11)
    active_key, active_digest = _key(active_data, asset="active")
    idle_key, idle_digest = _key(idle_data, asset="idle")
    active = _begin(store, active_key, len(active_data), active_digest)
    idle = _begin(store, idle_key, 11, idle_digest)
    # Both started three days ago; the active one received a part an hour ago.
    clock.now = NOW - timedelta(hours=1)
    store.receive_part(active, 1, active_data[:PART])
    clock.now = NOW

    aborted = store.abort_stale_uploads(NOW - timedelta(days=1))
    assert [(item.key, item.upload_id) for item in aborted] == [(idle_key, idle.upload_id)]
    with pytest.raises(UploadNotFound):
        store.upload_progress(idle)
    assert store.upload_progress(active).missing_part_numbers == (2,)


def test_stale_cleanup_removes_orphans_and_temporary_files(tmp_path: Path) -> None:
    store = _store(tmp_path / "media")
    data = _payload(10)
    key, digest = _key(data)
    live = _begin(store, key, 10, digest)
    staging = store.root / ".staging"
    old = (NOW - timedelta(days=3)).timestamp()
    orphans = [
        staging / f"{'a' * 32}.part",  # A part file whose state was never written.
        staging / f"{'b' * 32}.lock",
        staging / f"{'c' * 32}.p3.lock",
        staging / f"{live.upload_id}.0123456789abcdef.json.tmp",  # Crashed state write.
    ]
    recent_orphan = staging / f"{'d' * 32}.part"
    for path in [*orphans, recent_orphan]:
        path.write_bytes(b"x")
    for path in orphans:
        os.utime(path, (old, old))
    # The live upload's own files are old on disk too, but it is not stale by activity.
    for path in staging.glob(f"{live.upload_id}.*"):
        if not path.name.endswith(".tmp"):
            os.utime(path, (old, old))

    assert store.abort_stale_uploads(NOW - timedelta(days=1)) == []
    remaining = {path.name for path in staging.iterdir()}
    assert not remaining & {path.name for path in orphans}
    assert recent_orphan.name in remaining
    assert f"{live.upload_id}.part" in remaining and f"{live.upload_id}.json" in remaining
    assert store.upload_progress(live).missing_part_numbers == (1,)


def test_state_records_last_activity(tmp_path: Path) -> None:
    clock = _Clock(NOW)
    store = LocalDiskStore(tmp_path / "media", part_size=PART, clock=clock)
    data = _payload(PART)
    key, digest = _key(data)
    plan = _begin(store, key, len(data), digest)
    clock.now = NOW + timedelta(minutes=5)
    store.receive_part(plan, 1, data)
    state = json.loads((store.root / ".staging" / f"{plan.upload_id}.json").read_text())
    assert state["updated_at"] == (NOW + timedelta(minutes=5)).isoformat()


def test_delete_is_idempotent_and_prunes_empty_directories(tmp_path: Path) -> None:
    root = tmp_path / "media"
    store = _store(root)
    key = _completed(store, _payload(10))
    store.delete(key, workspace_id=WS)
    store.delete(key, workspace_id=WS)
    assert not (root / "w").exists()
    with pytest.raises(ObjectNotFound):
        store.open(key, workspace_id=WS)


def test_local_store_refuses_part_urls(tmp_path: Path) -> None:
    data = _payload(10)
    key, digest = _key(data)
    store = _store(tmp_path / "media")
    plan = _begin(store, key, 10, digest)
    with pytest.raises(UnsupportedOperation):
        store.part_upload_urls(plan)


def test_default_root_is_under_the_data_dir(tmp_path: Path) -> None:
    store = LocalDiskStore.from_settings(Settings(data_dir=tmp_path / "data"))
    assert store.root == (tmp_path / "data" / "media" / "uploads").resolve()
    assert store.scratch_dir == store.root / ".scratch"
