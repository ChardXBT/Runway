"""Media storage on the local disk, the ``local`` profile default (spec 02 §2).

Layout under the root (by default ``<data_dir>/mediastore``, outside the ``/media``
static mount so uploads are never served publicly):

- ``w/{workspace}/{asset}/{hash}.{ext}``: completed objects, named by their key;
- ``.staging/{upload_id}.part``: the bytes of an upload in progress;
- ``.staging/{upload_id}.json``: its plan, the parts that have arrived and the last activity;
- ``.staging/{upload_id}.lock`` and ``{upload_id}.p{n}.lock``: file locks (see below);
- ``.scratch/``: a same-volume place for :meth:`LocalDiskStore.copy_to_path` destinations,
  so the probe input can be a hard link instead of a copy.

Parts are written at their own offset, so they may arrive in any order and a dropped
connection costs only the part in flight. A part is recorded only after its bytes are flushed
to disk, so the state file never claims bytes the disk does not hold.

Concurrency: every read-modify-write of an upload's state holds ``{upload_id}.lock``, and a
part is written only while holding ``{upload_id}.p{n}.lock`` (taken without waiting, so a
second sender of the same part is refused). Both are operating-system file locks, so they hold
across store instances, threads and processes, and die with a crashed process.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, ExitStack, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from runway.mediastore.errors import (
    ChecksumMismatch,
    IncompleteUpload,
    InvalidMediaKey,
    InvalidUploadRequest,
    ObjectExists,
    ObjectNotFound,
    SizeMismatch,
    UnsupportedOperation,
    UploadNotFound,
)
from runway.mediastore.filelock import LockUnavailable, file_lock
from runway.mediastore.keys import check_workspace, normalize_sha256, parse_media_key
from runway.mediastore.store import (
    DEFAULT_PART_URL_TTL_SECONDS,
    PartReceipt,
    StaleUpload,
    StoreKind,
    UploadPlan,
    UploadProgress,
    check_upload_request,
    iter_file_blocks,
    replace_with_retry,
    write_verified,
)

if TYPE_CHECKING:
    from runway.config import Settings

DEFAULT_LOCAL_PART_BYTES = 8 * 1024 * 1024
_IO_BLOCK_BYTES = 1024 * 1024
_UPLOAD_ID_RE = re.compile(r"[0-9a-f]{32}")
_STAGING_DIR = ".staging"
_SCRATCH_DIR = ".scratch"


def _utc_now() -> datetime:
    return datetime.now(UTC)


class LocalDiskStore:
    kind: StoreKind = "local"

    def __init__(
        self,
        root: Path,
        *,
        part_size: int = DEFAULT_LOCAL_PART_BYTES,
        max_object_bytes: int | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if part_size <= 0:
            raise ValueError("part_size must be positive")
        self._root = root.resolve()
        self._staging = self._root / _STAGING_DIR
        self._part_size = part_size
        self._max_object_bytes = max_object_bytes
        self._clock = clock

    @classmethod
    def from_settings(cls, settings: Settings, **options: Any) -> LocalDiskStore:
        # Never under <data_dir>/media: the API serves that folder as a static
        # mount, and uploaded media must only leave through guarded routes.
        return cls(settings.resolved_data_dir / "mediastore", **options)

    @property
    def root(self) -> Path:
        return self._root

    @property
    def scratch_dir(self) -> Path:
        """Same-volume directory for :meth:`copy_to_path` destinations (hard links)."""
        return self._root / _SCRATCH_DIR

    # Uploads ---------------------------------------------------------------------------

    def begin_upload(
        self, key: str, size: int, content_type: str, *, sha256: str, workspace_id: str | int
    ) -> UploadPlan:
        content_type, digest = check_upload_request(key, size, content_type, sha256, workspace_id)
        if self._max_object_bytes is not None and size > self._max_object_bytes:
            raise InvalidUploadRequest(
                f"size {size} exceeds this store's limit of {self._max_object_bytes} bytes"
            )
        if self._object_path(key, workspace_id).exists():
            raise ObjectExists("an object already exists at this key")
        self._staging.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self._staging).free < size:
            raise InvalidUploadRequest("not enough free disk space for this upload")
        plan = UploadPlan(
            store="local",
            mode="api",
            key=key,
            upload_id=secrets.token_hex(16),
            size=size,
            sha256=digest,
            content_type=content_type,
            part_size=self._part_size,
            created_at=self._clock(),
        )
        with self._upload_lock(plan):
            with open(self._part_path(plan), "xb"):
                pass
            self._save_state(plan, {})
        return plan

    def part_upload_urls(
        self,
        plan: UploadPlan,
        part_numbers: Sequence[int] | None = None,
        *,
        ttl_seconds: int = DEFAULT_PART_URL_TTL_SECONDS,
    ) -> dict[int, str]:
        raise UnsupportedOperation("local uploads are sent through the API, not to part URLs")

    def receive_part(
        self, plan: UploadPlan, part_number: int, data: BinaryIO | bytes
    ) -> PartReceipt:
        expected = plan.part_length(part_number)
        offset = plan.part_offset(part_number)
        with ExitStack() as held:
            try:
                held.enter_context(
                    file_lock(self._part_lock_path(plan, part_number), blocking=False)
                )
            except LockUnavailable as exc:
                raise InvalidUploadRequest(f"part {part_number} is already being received") from exc
            with self._upload_lock(plan):
                parts = self._load_parts(plan)
                # Forget the part before overwriting it, so a failed rewrite cannot leave a
                # recorded part with half-new bytes.
                if parts.pop(part_number, None) is not None:
                    self._save_state(plan, parts)
            receipt = self._write_part(plan, part_number, offset, expected, data)
            with self._upload_lock(plan):
                parts = self._load_parts(plan)
                parts[part_number] = receipt
                self._save_state(plan, parts)
            return receipt

    def upload_progress(self, plan: UploadPlan) -> UploadProgress:
        with self._upload_lock(plan):
            parts = self._load_parts(plan)
        return UploadProgress.from_parts(plan, list(parts.values()))

    def complete_upload(self, plan: UploadPlan, parts: Sequence[PartReceipt] = ()) -> None:
        # begin_upload checked the plan's workspace; the key itself names it.
        final_path = self._object_path(plan.key, parse_media_key(plan.key).workspace_id)
        with self._upload_lock(plan):
            try:
                recorded = self._load_parts(plan)
            except UploadNotFound:
                if final_path.is_file() and final_path.stat().st_size == plan.size:
                    return
                raise
            progress = UploadProgress.from_parts(plan, list(recorded.values()))
            if not progress.is_complete:
                missing = progress.missing_part_numbers
                shown = ", ".join(str(number) for number in missing[:10])
                more = "" if len(missing) <= 10 else f" and {len(missing) - 10} more"
                raise IncompleteUpload(f"parts not received: {shown}{more}")
            for receipt in parts:
                if recorded.get(receipt.part_number) != receipt:
                    raise InvalidUploadRequest(
                        f"part {receipt.part_number} does not match the stored part"
                    )
            part_path = self._part_path(plan)
            actual_size = part_path.stat().st_size
            if actual_size != plan.size:
                raise SizeMismatch(f"upload holds {actual_size} bytes; expected {plan.size}")
            if _file_sha256(part_path) != plan.sha256:
                raise ChecksumMismatch("SHA-256 of the uploaded bytes does not match")
            if final_path.exists():
                raise ObjectExists("an object already exists at this key")
            final_path.parent.mkdir(parents=True, exist_ok=True)
            replace_with_retry(part_path, final_path)
            self._state_path(plan).unlink()
            self._remove_part_locks(plan)
        self._remove_quietly(self._upload_lock_path(plan))

    def abort_upload(self, plan: UploadPlan) -> None:
        with self._upload_lock(plan):
            self._part_path(plan).unlink(missing_ok=True)
            self._state_path(plan).unlink(missing_ok=True)
            self._remove_part_locks(plan)
        self._remove_quietly(self._upload_lock_path(plan))

    def abort_stale_uploads(self, older_than: datetime) -> list[StaleUpload]:
        """Abort uploads whose last activity is before ``older_than``; remove leftovers.

        Last activity is when the upload began or a part was last recorded. Leftovers are
        temporary state files and files of uploads that have no state file (a crash between
        creating the part file and the state, or an earlier interrupted cleanup), once their
        modification time is before ``older_than``.
        """
        if not self._staging.is_dir():
            return []
        aborted: list[StaleUpload] = []
        live_ids: set[str] = set()
        for state_path in sorted(self._staging.glob("*.json")):
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
                plan = UploadPlan.from_dict(payload["plan"])
                last_activity = _parse_time(payload.get("updated_at")) or plan.created_at
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if last_activity < older_than:
                self.abort_upload(plan)
                aborted.append(StaleUpload(plan.key, plan.upload_id, plan.created_at))
            else:
                live_ids.add(plan.upload_id)
        cutoff = older_than.timestamp()
        for leftover in self._staging.iterdir():
            upload_id = leftover.name.split(".", 1)[0]
            is_temporary = leftover.name.endswith(".tmp")
            if upload_id in live_ids and not is_temporary:
                continue
            if (self._staging / f"{upload_id}.json").exists() and not is_temporary:
                continue
            try:
                if leftover.is_file() and leftover.stat().st_mtime < cutoff:
                    leftover.unlink()
            except OSError:
                continue  # In use (a held lock on Windows) or already gone.
        return aborted

    # Objects ---------------------------------------------------------------------------

    def open(self, key: str, *, workspace_id: str | int) -> BinaryIO:
        path = self._object_path(key, workspace_id)
        try:
            return open(path, "rb")
        except FileNotFoundError as exc:
            raise ObjectNotFound("no object at this key") from exc

    def copy_to_path(
        self, key: str, destination: Path, *, workspace_id: str | int, sha256: str, size: int
    ) -> Path:
        """Hard-link (or copy, across volumes) the object to ``destination`` and verify it.

        A hard link shares the stored file's bytes: treat ``destination`` as read-only and
        delete it when done. Put it under :attr:`scratch_dir` to stay on the same volume.
        """
        source = self._object_path(key, workspace_id)
        if not source.is_file():
            raise ObjectNotFound("no object at this key")
        expected = normalize_sha256(sha256)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f"{destination.name}.{secrets.token_hex(8)}.tmp")
        try:
            os.link(source, temporary)
        except OSError:
            return write_verified(iter_file_blocks(source), destination, sha256=expected, size=size)
        try:
            actual = temporary.stat().st_size
            if actual != size:
                raise SizeMismatch(f"object holds {actual} bytes; expected {size}")
            if _file_sha256(temporary) != expected:
                raise ChecksumMismatch("SHA-256 of the stored bytes does not match the upload")
            replace_with_retry(temporary, destination)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return destination

    def put_object(
        self, key: str, data: bytes, *, workspace_id: str | int, content_type: str
    ) -> None:
        """Write a small server-made object (a poster frame) at ``key``."""
        path = self._object_path(key, workspace_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.{secrets.token_hex(8)}.tmp")
        try:
            temporary.write_bytes(data)
            replace_with_retry(temporary, path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def signed_get_url(self, key: str, ttl_seconds: int, *, workspace_id: str | int) -> str | None:
        self._object_path(key, workspace_id)
        return None

    def signed_preview_url(
        self, key: str, ttl_seconds: int, *, workspace_id: str | int, content_type: str
    ) -> str | None:
        self._object_path(key, workspace_id)
        return None

    def delete(self, key: str, *, workspace_id: str | int) -> None:
        path = self._object_path(key, workspace_id)
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != self._root and parent.is_relative_to(self._root):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    # Internals ---------------------------------------------------------------------------

    def _object_path(self, key: str, workspace_id: str | int) -> Path:
        check_workspace(key, workspace_id)
        candidate = (self._root / key).resolve()
        if not candidate.is_relative_to(self._root / "w"):
            raise InvalidMediaKey("storage key resolves outside the media root")
        return candidate

    def _upload_id(self, plan: UploadPlan) -> str:
        if plan.store != "local" or not _UPLOAD_ID_RE.fullmatch(plan.upload_id):
            raise UploadNotFound("upload plan does not belong to this store")
        return plan.upload_id

    def _part_path(self, plan: UploadPlan) -> Path:
        return self._staging / f"{self._upload_id(plan)}.part"

    def _state_path(self, plan: UploadPlan) -> Path:
        return self._staging / f"{self._upload_id(plan)}.json"

    def _upload_lock_path(self, plan: UploadPlan) -> Path:
        return self._staging / f"{self._upload_id(plan)}.lock"

    def _part_lock_path(self, plan: UploadPlan, part_number: int) -> Path:
        plan.part_offset(part_number)  # Validates the number.
        return self._staging / f"{self._upload_id(plan)}.p{part_number}.lock"

    def _upload_lock(self, plan: UploadPlan) -> AbstractContextManager[None]:
        return file_lock(self._upload_lock_path(plan))

    def _remove_part_locks(self, plan: UploadPlan) -> None:
        for path in self._staging.glob(f"{self._upload_id(plan)}.p*.lock"):
            self._remove_quietly(path)

    @staticmethod
    def _remove_quietly(path: Path) -> None:
        # Still open elsewhere (Windows) raises; abort_stale_uploads removes it later.
        with suppress(OSError):
            path.unlink(missing_ok=True)

    def _load_parts(self, plan: UploadPlan) -> dict[int, PartReceipt]:
        try:
            payload = json.loads(self._state_path(plan).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise UploadNotFound("no upload in progress for this plan") from exc
        if UploadPlan.from_dict(payload["plan"]) != plan:
            raise UploadNotFound("upload plan does not match the upload in progress")
        return {
            int(number): PartReceipt(int(number), int(item["size"]), str(item["etag"]))
            for number, item in payload["parts"].items()
        }

    def _save_state(self, plan: UploadPlan, parts: dict[int, PartReceipt]) -> None:
        payload = {
            "plan": plan.to_dict(),
            "updated_at": self._clock().isoformat(),
            "parts": {
                str(number): {"size": part.size, "etag": part.etag}
                for number, part in sorted(parts.items())
            },
        }
        state_path = self._state_path(plan)
        temporary = state_path.with_name(f"{plan.upload_id}.{secrets.token_hex(8)}.json.tmp")
        try:
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            replace_with_retry(temporary, state_path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    def _write_part(
        self,
        plan: UploadPlan,
        part_number: int,
        offset: int,
        expected: int,
        data: BinaryIO | bytes,
    ) -> PartReceipt:
        hasher = hashlib.sha256()
        written = 0
        part_path = self._part_path(plan)
        if not part_path.is_file():
            raise UploadNotFound("no upload in progress for this plan")
        with open(part_path, "r+b") as handle:
            handle.seek(offset)
            # Read one byte past the expected length to detect an oversized part.
            for block in _iter_blocks(data, expected + 1):
                if written + len(block) > expected:
                    raise InvalidUploadRequest(
                        f"part {part_number} is larger than its {expected} bytes"
                    )
                handle.write(block)
                hasher.update(block)
                written += len(block)
            if written != expected:
                raise InvalidUploadRequest(
                    f"part {part_number} has {written} bytes; expected {expected}"
                )
            handle.flush()
            os.fsync(handle.fileno())
        return PartReceipt(part_number=part_number, size=expected, etag=hasher.hexdigest())


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _iter_blocks(data: BinaryIO | bytes, limit: int) -> Iterator[bytes]:
    if isinstance(data, bytes | bytearray | memoryview):
        view = memoryview(data)[:limit]
        for start in range(0, len(view), _IO_BLOCK_BYTES):
            yield bytes(view[start : start + _IO_BLOCK_BYTES])
        return
    remaining = limit
    while remaining > 0:
        block = data.read(min(_IO_BLOCK_BYTES, remaining))
        if not block:
            return
        remaining -= len(block)
        yield block


def _file_sha256(path: Path) -> str:
    hasher = hashlib.sha256()
    for block in iter_file_blocks(path, _IO_BLOCK_BYTES):
        hasher.update(block)
    return hasher.hexdigest()


__all__ = ["DEFAULT_LOCAL_PART_BYTES", "LocalDiskStore"]
