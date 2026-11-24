"""The storage port (spec 02 §2) and the value types both stores share."""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import time
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, BinaryIO, Literal, Protocol

from runway.mediastore.errors import ChecksumMismatch, InvalidUploadRequest, SizeMismatch
from runway.mediastore.keys import check_workspace, normalize_sha256

StoreKind = Literal["local", "r2"]
UploadMode = Literal["api", "direct"]

# Runway's choice for how long a browser may use a part URL before asking for a new one.
DEFAULT_PART_URL_TTL_SECONDS = 3600

_MEDIA_MAJOR_TYPES = frozenset({"video", "image"})
_CONTENT_TYPE_RE = re.compile(r"[a-z0-9][a-z0-9!#$&^_.+-]{0,63}/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}")


@dataclass(frozen=True)
class PartReceipt:
    """One part the store holds. ``etag`` is the store's identifier for the part's bytes."""

    part_number: int
    size: int
    etag: str


@dataclass(frozen=True)
class UploadPlan:
    """Everything needed to continue, verify or abort one upload. Safe to persist as JSON."""

    store: StoreKind
    mode: UploadMode
    key: str
    upload_id: str
    size: int
    sha256: str
    content_type: str
    part_size: int
    created_at: datetime

    @property
    def part_count(self) -> int:
        return -(-self.size // self.part_size)

    def part_numbers(self) -> range:
        return range(1, self.part_count + 1)

    def part_offset(self, part_number: int) -> int:
        self._check_part_number(part_number)
        return (part_number - 1) * self.part_size

    def part_length(self, part_number: int) -> int:
        offset = self.part_offset(part_number)
        return min(self.part_size, self.size - offset)

    def _check_part_number(self, part_number: int) -> None:
        if not 1 <= part_number <= self.part_count:
            raise InvalidUploadRequest(
                f"part number must be between 1 and {self.part_count}, got {part_number}"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "store": self.store,
            "mode": self.mode,
            "key": self.key,
            "upload_id": self.upload_id,
            "size": self.size,
            "sha256": self.sha256,
            "content_type": self.content_type,
            "part_size": self.part_size,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> UploadPlan:
        store = payload["store"]
        mode = payload["mode"]
        if store not in ("local", "r2") or mode not in ("api", "direct"):
            raise InvalidUploadRequest("upload plan has an unknown store or mode")
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return cls(
            store=store,
            mode=mode,
            key=str(payload["key"]),
            upload_id=str(payload["upload_id"]),
            size=int(payload["size"]),
            sha256=str(payload["sha256"]),
            content_type=str(payload["content_type"]),
            part_size=int(payload["part_size"]),
            created_at=created_at,
        )


@dataclass(frozen=True)
class UploadProgress:
    """What has arrived, for ``GET /api/v1/media/{id}/upload`` and client resumption."""

    parts: tuple[PartReceipt, ...]
    missing_part_numbers: tuple[int, ...]
    # Inclusive byte ranges that arrived, merged: ((0, 16777215), ...).
    received_ranges: tuple[tuple[int, int], ...]
    bytes_received: int

    @property
    def is_complete(self) -> bool:
        return not self.missing_part_numbers

    @classmethod
    def from_parts(cls, plan: UploadPlan, parts: Sequence[PartReceipt]) -> UploadProgress:
        by_number = {
            part.part_number: part
            for part in parts
            if 1 <= part.part_number <= plan.part_count
            and part.size == plan.part_length(part.part_number)
        }
        ordered = tuple(by_number[number] for number in sorted(by_number))
        missing = tuple(number for number in plan.part_numbers() if number not in by_number)
        ranges: list[tuple[int, int]] = []
        for part in ordered:
            first = plan.part_offset(part.part_number)
            last = first + part.size - 1
            if ranges and ranges[-1][1] + 1 == first:
                ranges[-1] = (ranges[-1][0], last)
            else:
                ranges.append((first, last))
        return cls(
            parts=ordered,
            missing_part_numbers=missing,
            received_ranges=tuple(ranges),
            bytes_received=sum(part.size for part in ordered),
        )


@dataclass(frozen=True)
class StaleUpload:
    key: str
    upload_id: str
    initiated_at: datetime | None


class MediaStore(Protocol):
    """Storage for uploaded media. Implementations: ``LocalDiskStore`` and ``R2Store``.

    Every method that takes a key checks its format with
    :func:`runway.mediastore.keys.parse_media_key` and its ``w/{id}/`` prefix against
    ``workspace_id`` with :func:`runway.mediastore.keys.check_workspace`. Plans are created by
    ``begin_upload`` (which checks the workspace) and carry their key.
    """

    kind: StoreKind

    def begin_upload(
        self, key: str, size: int, content_type: str, *, sha256: str, workspace_id: str | int
    ) -> UploadPlan: ...

    def part_upload_urls(
        self,
        plan: UploadPlan,
        part_numbers: Sequence[int] | None = None,
        *,
        ttl_seconds: int = DEFAULT_PART_URL_TTL_SECONDS,
    ) -> dict[int, str]:
        """Signed URLs the browser uploads parts to (``direct`` mode only)."""
        ...

    def receive_part(
        self, plan: UploadPlan, part_number: int, data: BinaryIO | bytes
    ) -> PartReceipt:
        """Store one part sent through the API (``api`` mode only)."""
        ...

    def upload_progress(self, plan: UploadPlan) -> UploadProgress: ...

    def complete_upload(self, plan: UploadPlan, parts: Sequence[PartReceipt] = ()) -> None:
        """Assemble the object at ``plan.key``.

        ``parts`` are the receipts the client reported; each must match what the store
        recorded. An empty sequence means "use the store's own record". Idempotent: calling
        it again after success is a no-op. ``LocalDiskStore`` verifies SHA-256 here;
        ``R2Store`` cannot without downloading, so its hash is verified by
        :meth:`copy_to_path`, which the probe job must call before trusting the asset.
        """
        ...

    def abort_upload(self, plan: UploadPlan) -> None: ...

    def abort_stale_uploads(self, older_than: datetime) -> list[StaleUpload]:
        """Abort uploads with no activity since ``older_than`` and remove leftovers."""
        ...

    def open(self, key: str, *, workspace_id: str | int) -> BinaryIO:
        """A seekable binary reader; seek then read for ranged access."""
        ...

    def copy_to_path(
        self, key: str, destination: Path, *, workspace_id: str | int, sha256: str, size: int
    ) -> Path:
        """Put the object's bytes at a server-chosen ``destination`` and verify them.

        Raises ``SizeMismatch`` or ``ChecksumMismatch`` (leaving no file behind) when the
        bytes are not the ones the client declared. This is the SHA-256 check of spec 02 §4
        step 3 for R2, and the input for the probe.
        """
        ...

    def put_object(
        self, key: str, data: bytes, *, workspace_id: str | int, content_type: str
    ) -> None:
        """Write a small server-made object directly at ``key`` (a poster frame).

        Uploads from clients go through :meth:`begin_upload`/`complete_upload`;
        this is for bytes the server produced itself, so no plan exists.
        """
        ...

    def signed_get_url(self, key: str, ttl_seconds: int, *, workspace_id: str | int) -> str | None:
        """Plain signed URL for a platform to fetch (Instagram). ``None`` when unavailable."""
        ...

    def signed_preview_url(
        self, key: str, ttl_seconds: int, *, workspace_id: str | int, content_type: str
    ) -> str | None:
        """Signed URL for an in-app preview.

        The response is forced to ``content_type`` (from the probe) and
        ``Content-Disposition: attachment``, so the URL cannot be opened as a page.
        """
        ...

    def delete(self, key: str, *, workspace_id: str | int) -> None:
        """Delete the object. Deleting a missing object is not an error."""
        ...


def normalize_content_type(content_type: str) -> str:
    """Lowercased ``type/subtype``; only ``video/*`` and ``image/*`` (not XML images)."""
    value = content_type.split(";", 1)[0].strip().lower()
    if not _CONTENT_TYPE_RE.fullmatch(value):
        raise InvalidUploadRequest("content type must look like type/subtype")
    major, _, subtype = value.partition("/")
    if major not in _MEDIA_MAJOR_TYPES or subtype.endswith("+xml"):
        raise InvalidUploadRequest("only video and image uploads are accepted")
    return value


def check_upload_request(
    key: str, size: int, content_type: str, sha256: str, workspace_id: str | int
) -> tuple[str, str]:
    """Validate a ``begin_upload`` request; return the normalized content type and digest."""
    parsed = check_workspace(key, workspace_id)
    if size <= 0:
        raise InvalidUploadRequest("size must be a positive number of bytes")
    digest = normalize_sha256(sha256)
    if not digest.startswith(parsed.digest_prefix):
        raise InvalidUploadRequest("the key's hash prefix does not match sha256")
    return normalize_content_type(content_type), digest


_VERIFY_BLOCK_BYTES = 1024 * 1024


def write_verified(blocks: Iterable[bytes], destination: Path, *, sha256: str, size: int) -> Path:
    """Write ``blocks`` to ``destination``, checking the size and SHA-256 as they stream.

    The bytes go to a uniquely named temporary file next to ``destination`` and are renamed
    into place only when both checks pass; on any failure nothing is left behind.
    """
    expected = normalize_sha256(sha256)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f"{destination.name}.{secrets.token_hex(8)}.tmp")
    hasher = hashlib.sha256()
    written = 0
    try:
        with open(temporary, "xb") as handle:
            for block in blocks:
                written += len(block)
                if written > size:
                    raise SizeMismatch(f"object holds more than the expected {size} bytes")
                hasher.update(block)
                handle.write(block)
        if written != size:
            raise SizeMismatch(f"object holds {written} bytes; expected {size}")
        if hasher.hexdigest() != expected:
            raise ChecksumMismatch("SHA-256 of the stored bytes does not match the upload")
        replace_with_retry(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def iter_file_blocks(path: Path, block_size: int = _VERIFY_BLOCK_BYTES) -> Iterator[bytes]:
    with open(path, "rb") as handle:
        while block := handle.read(block_size):
            yield block


# Windows refuses to replace a file another process has open (a virus scanner, an indexer, a
# reader that has not closed yet) with PermissionError; such holds last milliseconds.
_REPLACE_ATTEMPTS = 6
_REPLACE_BACKOFF_SECONDS = 0.05


def replace_with_retry(source: Path, destination: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_BACKOFF_SECONDS * (attempt + 1))
