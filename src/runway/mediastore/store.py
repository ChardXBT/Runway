"""The storage port (spec 02 §2) and the value types both stores share."""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, BinaryIO, Literal, Protocol

from runway.mediastore.errors import InvalidUploadRequest
from runway.mediastore.keys import normalize_sha256, parse_media_key

StoreKind = Literal["local", "r2"]
UploadMode = Literal["api", "direct"]

# Runway's choice for how long a browser may use a part URL before asking for a new one.
DEFAULT_PART_URL_TTL_SECONDS = 3600

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

    Every method validates the key with :func:`runway.mediastore.keys.parse_media_key`.
    """

    kind: StoreKind

    def begin_upload(
        self, key: str, size: int, content_type: str, *, sha256: str
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
        """Verify and publish the object at ``plan.key``.

        ``parts`` are the receipts the client reported; each must match what the store
        recorded. An empty sequence means "use the store's own record". Idempotent: calling
        it again after success is a no-op.
        """
        ...

    def abort_upload(self, plan: UploadPlan) -> None: ...

    def abort_stale_uploads(self, older_than: datetime) -> list[StaleUpload]: ...

    def open(self, key: str) -> BinaryIO:
        """A seekable binary reader; seek then read for ranged access."""
        ...

    def signed_get_url(self, key: str, ttl_seconds: int) -> str | None: ...

    def delete(self, key: str) -> None:
        """Delete the object. Deleting a missing object is not an error."""
        ...


def normalize_content_type(content_type: str) -> str:
    value = content_type.split(";", 1)[0].strip().lower()
    if not _CONTENT_TYPE_RE.fullmatch(value):
        raise InvalidUploadRequest("content type must look like type/subtype")
    return value


def check_upload_request(key: str, size: int, content_type: str, sha256: str) -> tuple[str, str]:
    """Validate a ``begin_upload`` request; return the normalized content type and digest."""
    parsed = parse_media_key(key)
    if size <= 0:
        raise InvalidUploadRequest("size must be a positive number of bytes")
    digest = normalize_sha256(sha256)
    if not digest.startswith(parsed.digest_prefix):
        raise InvalidUploadRequest("the key's hash prefix does not match sha256")
    return normalize_content_type(content_type), digest
