"""Media storage on the local disk, the ``local`` profile default (spec 02 §2).

Layout under the root (by default ``<data_dir>/media/uploads``):

- ``w/{workspace}/{asset}/{hash}.{ext}``: completed objects, named by their key;
- ``.staging/{upload_id}.part`` and ``.staging/{upload_id}.json``: an upload in progress and
  the parts that have arrived.

Parts are written at their own offset, so they may arrive in any order and a dropped
connection costs only the part in flight. A part is recorded only after its bytes are flushed
to disk, so the state file never claims bytes the disk does not hold.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import threading
from collections.abc import Callable, Iterator, Sequence
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
from runway.mediastore.keys import parse_media_key
from runway.mediastore.store import (
    DEFAULT_PART_URL_TTL_SECONDS,
    PartReceipt,
    StaleUpload,
    StoreKind,
    UploadPlan,
    UploadProgress,
    check_upload_request,
)

if TYPE_CHECKING:
    from runway.config import Settings

DEFAULT_LOCAL_PART_BYTES = 8 * 1024 * 1024
_IO_BLOCK_BYTES = 1024 * 1024
_UPLOAD_ID_RE = re.compile(r"[0-9a-f]{32}")
_STAGING_DIR = ".staging"


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
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._parts_in_flight: set[tuple[str, int]] = set()

    @classmethod
    def from_settings(cls, settings: Settings, **options: Any) -> LocalDiskStore:
        return cls(settings.resolved_data_dir / "media" / "uploads", **options)

    @property
    def root(self) -> Path:
        return self._root

    # Uploads ---------------------------------------------------------------------------

    def begin_upload(self, key: str, size: int, content_type: str, *, sha256: str) -> UploadPlan:
        content_type, digest = check_upload_request(key, size, content_type, sha256)
        if self._max_object_bytes is not None and size > self._max_object_bytes:
            raise InvalidUploadRequest(
                f"size {size} exceeds this store's limit of {self._max_object_bytes} bytes"
            )
        if self._object_path(key).exists():
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
        flight = (plan.upload_id, part_number)
        with self._guard:
            if flight in self._parts_in_flight:
                raise InvalidUploadRequest(f"part {part_number} is already being received")
            self._parts_in_flight.add(flight)
        try:
            with self._lock_for(plan):
                parts = self._load_parts(plan)
                # Forget the part before overwriting it, so a failed rewrite cannot leave a
                # recorded part with half-new bytes.
                if parts.pop(part_number, None) is not None:
                    self._save_state(plan, parts)
            receipt = self._write_part(plan, part_number, offset, expected, data)
            with self._lock_for(plan):
                parts = self._load_parts(plan)
                parts[part_number] = receipt
                self._save_state(plan, parts)
            return receipt
        finally:
            with self._guard:
                self._parts_in_flight.discard(flight)

    def upload_progress(self, plan: UploadPlan) -> UploadProgress:
        with self._lock_for(plan):
            parts = self._load_parts(plan)
        return UploadProgress.from_parts(plan, list(parts.values()))

    def complete_upload(self, plan: UploadPlan, parts: Sequence[PartReceipt] = ()) -> None:
        final_path = self._object_path(plan.key)
        with self._lock_for(plan):
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
            os.replace(part_path, final_path)
            self._state_path(plan).unlink()

    def abort_upload(self, plan: UploadPlan) -> None:
        with self._lock_for(plan):
            self._part_path(plan).unlink(missing_ok=True)
            self._state_path(plan).unlink(missing_ok=True)

    def abort_stale_uploads(self, older_than: datetime) -> list[StaleUpload]:
        if not self._staging.is_dir():
            return []
        aborted: list[StaleUpload] = []
        for state_path in sorted(self._staging.glob("*.json")):
            try:
                payload = json.loads(state_path.read_text(encoding="utf-8"))
                plan = UploadPlan.from_dict(payload["plan"])
            except (OSError, ValueError, KeyError, TypeError):
                continue
            if plan.created_at < older_than:
                self.abort_upload(plan)
                aborted.append(StaleUpload(plan.key, plan.upload_id, plan.created_at))
        return aborted

    # Objects ---------------------------------------------------------------------------

    def open(self, key: str) -> BinaryIO:
        path = self._object_path(key)
        try:
            return open(path, "rb")
        except FileNotFoundError as exc:
            raise ObjectNotFound("no object at this key") from exc

    def signed_get_url(self, key: str, ttl_seconds: int) -> str | None:
        self._object_path(key)
        return None

    def delete(self, key: str) -> None:
        path = self._object_path(key)
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != self._root and parent.is_relative_to(self._root):
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    # Internals ---------------------------------------------------------------------------

    def _object_path(self, key: str) -> Path:
        parse_media_key(key)
        candidate = (self._root / key).resolve()
        if not candidate.is_relative_to(self._root) or candidate.is_relative_to(self._staging):
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

    def _lock_for(self, plan: UploadPlan) -> threading.Lock:
        upload_id = self._upload_id(plan)
        with self._guard:
            return self._locks.setdefault(upload_id, threading.Lock())

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
            "parts": {
                str(number): {"size": part.size, "etag": part.etag}
                for number, part in sorted(parts.items())
            },
        }
        state_path = self._state_path(plan)
        temporary = state_path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(temporary, state_path)

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
    with open(path, "rb") as handle:
        while block := handle.read(_IO_BLOCK_BYTES):
            hasher.update(block)
    return hasher.hexdigest()


__all__ = ["DEFAULT_LOCAL_PART_BYTES", "LocalDiskStore"]
