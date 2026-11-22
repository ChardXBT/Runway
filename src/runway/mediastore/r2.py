"""Cloudflare R2 media storage through its S3-compatible API (spec 02 §2, §4, §6).

The browser uploads parts straight to R2 with presigned part URLs, so media bytes never pass
through the API process. Each part URL signs ``content-length``, which binds the URL to the
part's planned length; ``complete_upload`` also re-reads the part list from R2 and checks
every size before assembling the object, and checks the object's length afterwards.

R2 cannot check the declared SHA-256 at completion (part ETags are R2's MD5s), so
:meth:`R2Store.copy_to_path` streams the object to the probe job's server-named file and
verifies the size and SHA-256 on the way; a mismatch rejects the asset. Every object method
takes ``workspace_id`` and refuses keys outside ``w/{workspace_id}/``.

Requests use path-style URLs (``https://<account>.r2.cloudflarestorage.com/<bucket>/<key>``),
region ``auto`` and service ``s3``.
"""

from __future__ import annotations

import io
import re
import xml.etree.ElementTree as ElementTree
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO
from xml.sax.saxutils import escape

import httpx

from runway.mediastore.errors import (
    IncompleteUpload,
    InvalidMediaKey,
    InvalidUploadRequest,
    ObjectNotFound,
    SizeMismatch,
    StorageBackendError,
    UnsupportedOperation,
    UploadNotFound,
)
from runway.mediastore.keys import check_workspace, parse_media_key
from runway.mediastore.sigv4 import (
    SigV4Credentials,
    presign_url,
    sha256_hex,
    sign_headers,
    uri_encode,
)
from runway.mediastore.store import (
    DEFAULT_PART_URL_TTL_SECONDS,
    PartReceipt,
    StaleUpload,
    StoreKind,
    UploadPlan,
    UploadProgress,
    check_upload_request,
    normalize_content_type,
    write_verified,
)
from runway.platforms.base import safe_error_summary
from runway.platforms.limits import R2_STORAGE, MiB

_ACCOUNT_RE = re.compile(r"[A-Za-z0-9]{1,64}")
_BUCKET_RE = re.compile(r"[a-z0-9][a-z0-9-]{1,61}[a-z0-9]")
_LIST_PAGE_SIZE = 1000
_READ_BUFFER_BYTES = 1 * MiB
_SERVICE = "s3"


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class R2Config:
    account_id: str
    bucket: str
    access_key_id: str
    secret_access_key: str = field(repr=False)
    # Override for S3-compatible test doubles or a jurisdiction endpoint (``…eu.r2…``).
    endpoint: str | None = None
    region: str = "auto"

    def __post_init__(self) -> None:
        if not _ACCOUNT_RE.fullmatch(self.account_id):
            raise ValueError("R2 account id must be letters and digits")
        if not _BUCKET_RE.fullmatch(self.bucket):
            raise ValueError("R2 bucket name must be 3-63 lowercase letters, digits or hyphens")
        if not self.access_key_id or not self.secret_access_key:
            raise ValueError("R2 access key id and secret are required")
        if self.endpoint is not None and not self.endpoint.startswith("https://"):
            raise ValueError("R2 endpoint must use https")

    @property
    def endpoint_url(self) -> str:
        if self.endpoint is not None:
            return self.endpoint.rstrip("/")
        return f"https://{self.account_id}.r2.cloudflarestorage.com"

    @property
    def credentials(self) -> SigV4Credentials:
        return SigV4Credentials(self.access_key_id, self.secret_access_key)


class R2Store:
    kind: StoreKind = "r2"

    def __init__(
        self,
        config: R2Config,
        *,
        client: httpx.Client | None = None,
        part_size: int = R2_STORAGE.default_part_bytes,
        max_object_bytes: int | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        if not R2_STORAGE.min_part_bytes <= part_size <= R2_STORAGE.max_part_bytes:
            raise ValueError("part_size is outside R2's multipart part limits")
        self._config = config
        self._owns_client = client is None
        self._client = client or httpx.Client(timeout=httpx.Timeout(30.0, read=120.0))
        self._part_size = part_size
        self._max_object_bytes = max_object_bytes
        self._clock = clock

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    # Uploads ---------------------------------------------------------------------------

    def begin_upload(
        self, key: str, size: int, content_type: str, *, sha256: str, workspace_id: str | int
    ) -> UploadPlan:
        content_type, digest = check_upload_request(key, size, content_type, sha256, workspace_id)
        if self._max_object_bytes is not None and size > self._max_object_bytes:
            raise InvalidUploadRequest(
                f"size {size} exceeds this store's limit of {self._max_object_bytes} bytes"
            )
        part_size = self._part_size_for(size)
        response = self._send(
            "POST",
            self._object_url(key, [("uploads", "")]),
            operation="create multipart upload",
            headers={"content-type": content_type},
        )
        upload_id = _child_text(_parse_xml(response), "UploadId")
        if not upload_id:
            raise StorageBackendError("R2 create multipart upload returned no UploadId")
        return UploadPlan(
            store="r2",
            mode="direct",
            key=key,
            upload_id=upload_id,
            size=size,
            sha256=digest,
            content_type=content_type,
            part_size=part_size,
            created_at=self._clock(),
        )

    def part_upload_urls(
        self,
        plan: UploadPlan,
        part_numbers: Sequence[int] | None = None,
        *,
        ttl_seconds: int = DEFAULT_PART_URL_TTL_SECONDS,
    ) -> dict[int, str]:
        self._check_plan(plan)
        self._check_ttl(ttl_seconds)
        numbers = list(plan.part_numbers()) if part_numbers is None else list(part_numbers)
        now = self._clock()
        urls: dict[int, str] = {}
        for number in numbers:
            length = plan.part_length(number)
            url = self._object_url(
                plan.key, [("partNumber", str(number)), ("uploadId", plan.upload_id)]
            )
            urls[number] = presign_url(
                "PUT",
                url,
                credentials=self._config.credentials,
                region=self._config.region,
                service=_SERVICE,
                now=now,
                expires_seconds=ttl_seconds,
                signed_headers={"content-length": str(length)},
            )
        return urls

    def receive_part(
        self, plan: UploadPlan, part_number: int, data: BinaryIO | bytes
    ) -> PartReceipt:
        raise UnsupportedOperation("R2 parts are uploaded by the browser to part URLs")

    def upload_progress(self, plan: UploadPlan) -> UploadProgress:
        self._check_plan(plan)
        return UploadProgress.from_parts(plan, self._list_parts(plan))

    def complete_upload(self, plan: UploadPlan, parts: Sequence[PartReceipt] = ()) -> None:
        self._check_plan(plan)
        try:
            listed = self._list_parts(plan)
        except UploadNotFound:
            # Already completed by an earlier call whose response was lost.
            if self._object_size(plan.key) == plan.size:
                return
            raise
        by_number = {part.part_number: part for part in listed}
        missing = [number for number in plan.part_numbers() if number not in by_number]
        if missing:
            shown = ", ".join(str(number) for number in missing[:10])
            more = "" if len(missing) <= 10 else f" and {len(missing) - 10} more"
            raise IncompleteUpload(f"parts not received: {shown}{more}")
        for number in plan.part_numbers():
            if by_number[number].size != plan.part_length(number):
                raise SizeMismatch(
                    f"part {number} holds {by_number[number].size} bytes; "
                    f"expected {plan.part_length(number)}"
                )
        for receipt in parts:
            stored = by_number.get(receipt.part_number)
            if stored is None or stored.etag != _strip_etag(receipt.etag):
                raise InvalidUploadRequest(
                    f"part {receipt.part_number} does not match the stored part"
                )
        body = "".join(
            f"<Part><PartNumber>{number}</PartNumber>"
            f"<ETag>&quot;{escape(by_number[number].etag)}&quot;</ETag></Part>"
            for number in plan.part_numbers()
        )
        try:
            response = self._send(
                "POST",
                self._object_url(plan.key, [("uploadId", plan.upload_id)]),
                operation="complete multipart upload",
                headers={"content-type": "application/xml"},
                body=f"<CompleteMultipartUpload>{body}</CompleteMultipartUpload>".encode(),
                not_found=UploadNotFound,
            )
        except UploadNotFound:
            # A parallel completion finished between our ListParts and this request.
            if self._object_size(plan.key) == plan.size:
                return
            raise
        # S3 can report a failed completion with status 200 and an <Error> body.
        root = _parse_xml(response)
        if _local_name(root.tag) == "Error":
            raise StorageBackendError(
                f"R2 complete multipart upload failed: {_child_text(root, 'Code') or 'error'}",
                status=response.status_code,
                retryable=True,
            )
        actual = self._object_size(plan.key)
        if actual != plan.size:
            self._delete(plan.key)
            raise SizeMismatch(f"object holds {actual} bytes; expected {plan.size}")

    def abort_upload(self, plan: UploadPlan) -> None:
        self._check_plan(plan)
        self._abort(plan.key, plan.upload_id)

    def abort_stale_uploads(self, older_than: datetime) -> list[StaleUpload]:
        """Abort multipart uploads under ``w/`` started before ``older_than``."""
        aborted: list[StaleUpload] = []
        for upload in self._list_multipart_uploads(prefix="w/"):
            if upload.initiated_at is None or upload.initiated_at >= older_than:
                continue
            try:
                parse_media_key(upload.key)
            except InvalidMediaKey:
                continue
            self._abort(upload.key, upload.upload_id)
            aborted.append(upload)
        return aborted

    # Objects ---------------------------------------------------------------------------

    def open(self, key: str, *, workspace_id: str | int) -> BinaryIO:
        check_workspace(key, workspace_id)
        response = self._send(
            "HEAD", self._object_url(key), operation="head object", not_found=ObjectNotFound
        )
        size = int(response.headers.get("content-length", "0"))
        etag = response.headers.get("etag")
        raw = _RangedObjectReader(self, key, size, etag)
        return io.BufferedReader(raw, buffer_size=_READ_BUFFER_BYTES)

    def object_size(self, key: str, *, workspace_id: str | int) -> int | None:
        check_workspace(key, workspace_id)
        return self._object_size(key)

    def copy_to_path(
        self, key: str, destination: Path, *, workspace_id: str | int, sha256: str, size: int
    ) -> Path:
        """Download the object to ``destination``, verifying size and SHA-256 as it streams.

        This is where an R2 upload's declared hash is checked (spec 02 §4 step 3): part
        ETags are MD5s chosen by R2, so ``complete_upload`` cannot check it. On
        ``ChecksumMismatch`` or ``SizeMismatch`` no file is left; the caller should reject the
        asset and delete the object.
        """
        check_workspace(key, workspace_id)
        with self._stream(
            "GET", self._object_url(key), operation="get object", not_found=ObjectNotFound
        ) as response:
            return write_verified(
                response.iter_bytes(_READ_BUFFER_BYTES), destination, sha256=sha256, size=size
            )

    def signed_get_url(self, key: str, ttl_seconds: int, *, workspace_id: str | int) -> str | None:
        """Plain presigned GET, for a platform to fetch (the Instagram container URL)."""
        check_workspace(key, workspace_id)
        return self._presign_get(key, ttl_seconds, [])

    def signed_preview_url(
        self, key: str, ttl_seconds: int, *, workspace_id: str | int, content_type: str
    ) -> str | None:
        """Presigned GET for previews: the probed type and ``attachment`` are forced."""
        check_workspace(key, workspace_id)
        overrides = [
            ("response-content-disposition", "attachment"),
            ("response-content-type", normalize_content_type(content_type)),
        ]
        return self._presign_get(key, ttl_seconds, overrides)

    def delete(self, key: str, *, workspace_id: str | int) -> None:
        check_workspace(key, workspace_id)
        self._delete(key)

    # Internals ---------------------------------------------------------------------------

    def _presign_get(self, key: str, ttl_seconds: int, query: list[tuple[str, str]]) -> str:
        self._check_ttl(ttl_seconds)
        return presign_url(
            "GET",
            self._object_url(key, query),
            credentials=self._config.credentials,
            region=self._config.region,
            service=_SERVICE,
            now=self._clock(),
            expires_seconds=ttl_seconds,
        )

    def _delete(self, key: str) -> None:
        self._send(
            "DELETE",
            self._object_url(key),
            operation="delete object",
            expected=(200, 204, 404),
        )

    def _part_size_for(self, size: int) -> int:
        needed = -(-size // R2_STORAGE.max_parts)
        needed = -(-needed // MiB) * MiB
        part_size = max(self._part_size, needed)
        if part_size > R2_STORAGE.max_part_bytes:
            raise InvalidUploadRequest("file is too large for an R2 multipart upload")
        return part_size

    def _check_plan(self, plan: UploadPlan) -> None:
        if plan.store != "r2" or not plan.upload_id:
            raise UploadNotFound("upload plan does not belong to this store")
        parse_media_key(plan.key)

    @staticmethod
    def _check_ttl(ttl_seconds: int) -> None:
        max_ttl = int(R2_STORAGE.presigned_url_max_ttl.total_seconds())
        if not 1 <= ttl_seconds <= max_ttl:
            raise ValueError(f"ttl_seconds must be between 1 and {max_ttl}")

    def _object_url(self, key: str, query: list[tuple[str, str]] | None = None) -> str:
        parse_media_key(key)
        url = (
            f"{self._config.endpoint_url}/{self._config.bucket}/{uri_encode(key, keep_slash=True)}"
        )
        return _with_query(url, query)

    def _bucket_url(self, query: list[tuple[str, str]]) -> str:
        return _with_query(f"{self._config.endpoint_url}/{self._config.bucket}", query)

    def _send(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
        expected: tuple[int, ...] = (200,),
        not_found: type[Exception] | None = None,
    ) -> httpx.Response:
        signed = self._sign(method, url, headers, body)
        try:
            response = self._client.request(method, url, headers=signed, content=body or None)
        except httpx.HTTPError as exc:
            raise StorageBackendError(
                f"R2 {operation} failed: {safe_error_summary(exc)}", retryable=True
            ) from exc
        self._check_status(response, operation, expected, not_found)
        return response

    @contextmanager
    def _stream(
        self,
        method: str,
        url: str,
        *,
        operation: str,
        headers: Mapping[str, str] | None = None,
        expected: tuple[int, ...] = (200,),
        not_found: type[Exception] | None = None,
    ) -> Iterator[httpx.Response]:
        """Like ``_send`` but the body is not read; leaving the block closes the response."""
        signed = self._sign(method, url, headers, b"")
        try:
            with self._client.stream(method, url, headers=signed) as response:
                if response.status_code not in expected:
                    response.read()  # Error bodies are small XML documents.
                self._check_status(response, operation, expected, not_found)
                yield response
        except httpx.HTTPError as exc:
            raise StorageBackendError(
                f"R2 {operation} failed: {safe_error_summary(exc)}", retryable=True
            ) from exc

    def _sign(
        self, method: str, url: str, headers: Mapping[str, str] | None, body: bytes
    ) -> dict[str, str]:
        return sign_headers(
            method,
            url,
            headers or {},
            payload_hash=sha256_hex(body),
            credentials=self._config.credentials,
            region=self._config.region,
            service=_SERVICE,
            now=self._clock(),
        )

    @staticmethod
    def _check_status(
        response: httpx.Response,
        operation: str,
        expected: tuple[int, ...],
        not_found: type[Exception] | None,
    ) -> None:
        if response.status_code in expected:
            return
        if response.status_code == 404 and not_found is not None:
            raise not_found(f"R2 {operation}: not found")
        code = _error_code(response)
        raise StorageBackendError(
            f"R2 {operation} returned HTTP {response.status_code}" + (f" ({code})" if code else ""),
            status=response.status_code,
            retryable=response.status_code >= 500 or response.status_code == 429,
        )

    def _object_size(self, key: str) -> int | None:
        response = self._send(
            "HEAD", self._object_url(key), operation="head object", expected=(200, 404)
        )
        if response.status_code == 404:
            return None
        return int(response.headers.get("content-length", "0"))

    def _read_range(self, key: str, first: int, last: int, etag: str | None) -> bytes:
        headers = {"range": f"bytes={first}-{last}"}
        if etag:
            headers["if-match"] = etag
        wanted = last - first + 1
        with self._stream(
            "GET",
            self._object_url(key),
            operation="get object range",
            headers=headers,
            expected=(200, 206),
            not_found=ObjectNotFound,
        ) as response:
            if response.status_code == 200 and first > 0:
                # The server ignored Range and is sending the whole object; skipping to
                # ``first`` could mean downloading gigabytes, so refuse instead.
                raise StorageBackendError("R2 ignored the Range header of a ranged read")
            data = bytearray()
            for block in response.iter_bytes():
                data += block[: wanted - len(data)]
                if len(data) >= wanted:
                    break
        if len(data) != wanted:
            raise StorageBackendError("R2 returned a short range read", retryable=True)
        return bytes(data)

    def _list_parts(self, plan: UploadPlan) -> list[PartReceipt]:
        parts: list[PartReceipt] = []
        marker = "0"
        while True:
            response = self._send(
                "GET",
                self._object_url(
                    plan.key,
                    [
                        ("max-parts", str(_LIST_PAGE_SIZE)),
                        ("part-number-marker", marker),
                        ("uploadId", plan.upload_id),
                    ],
                ),
                operation="list parts",
                not_found=UploadNotFound,
            )
            root = _parse_xml(response)
            for element in _children(root, "Part"):
                parts.append(
                    PartReceipt(
                        part_number=int(_child_text(element, "PartNumber") or "0"),
                        size=int(_child_text(element, "Size") or "0"),
                        etag=_strip_etag(_child_text(element, "ETag") or ""),
                    )
                )
            next_marker = _child_text(root, "NextPartNumberMarker")
            if _child_text(root, "IsTruncated") != "true" or not next_marker:
                return parts
            marker = next_marker

    def _list_multipart_uploads(self, *, prefix: str) -> list[StaleUpload]:
        uploads: list[StaleUpload] = []
        key_marker = ""
        upload_id_marker = ""
        while True:
            query = [("uploads", ""), ("prefix", prefix), ("max-uploads", str(_LIST_PAGE_SIZE))]
            if key_marker:
                query += [("key-marker", key_marker), ("upload-id-marker", upload_id_marker)]
            response = self._send(
                "GET", self._bucket_url(query), operation="list multipart uploads"
            )
            root = _parse_xml(response)
            for element in _children(root, "Upload"):
                uploads.append(
                    StaleUpload(
                        key=_child_text(element, "Key") or "",
                        upload_id=_child_text(element, "UploadId") or "",
                        initiated_at=_parse_timestamp(_child_text(element, "Initiated")),
                    )
                )
            if _child_text(root, "IsTruncated") != "true":
                return uploads
            key_marker = _child_text(root, "NextKeyMarker") or ""
            upload_id_marker = _child_text(root, "NextUploadIdMarker") or ""
            if not key_marker:
                return uploads

    def _abort(self, key: str, upload_id: str) -> None:
        self._send(
            "DELETE",
            self._object_url(key, [("uploadId", upload_id)]),
            operation="abort multipart upload",
            expected=(200, 204, 404),
        )


class _RangedObjectReader(io.RawIOBase):
    """Seekable raw reader that turns each read into one ranged ``GET``."""

    def __init__(self, store: R2Store, key: str, size: int, etag: str | None) -> None:
        super().__init__()
        self._store = store
        self._key = key
        self._size = size
        # Pinning the ETag makes a replaced object fail loudly instead of mixing versions.
        self._etag = etag
        self._position = 0

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._position

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        if whence == io.SEEK_SET:
            position = offset
        elif whence == io.SEEK_CUR:
            position = self._position + offset
        elif whence == io.SEEK_END:
            position = self._size + offset
        else:
            raise ValueError("invalid whence")
        if position < 0:
            raise ValueError("negative seek position")
        self._position = position
        return position

    def readinto(self, buffer: memoryview | bytearray) -> int:  # type: ignore[override]
        wanted = len(buffer)
        if wanted == 0 or self._position >= self._size:
            return 0
        last = min(self._position + wanted, self._size) - 1
        data = self._store._read_range(self._key, self._position, last, self._etag)
        buffer[: len(data)] = data
        self._position += len(data)
        return len(data)


def _with_query(url: str, query: list[tuple[str, str]] | None) -> str:
    if not query:
        return url
    encoded = "&".join(f"{uri_encode(name)}={uri_encode(value)}" for name, value in query)
    return f"{url}?{encoded}"


def _strip_etag(value: str) -> str:
    return value.strip().strip('"')


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _children(element: ElementTree.Element, name: str) -> list[ElementTree.Element]:
    return [child for child in element if _local_name(child.tag) == name]


def _child_text(element: ElementTree.Element, name: str) -> str | None:
    for child in element:
        if _local_name(child.tag) == name:
            return (child.text or "").strip()
    return None


def _parse_xml(response: httpx.Response) -> ElementTree.Element:
    try:
        return ElementTree.fromstring(response.content)
    except ElementTree.ParseError as exc:
        raise StorageBackendError("R2 returned a response that is not XML") from exc


def _error_code(response: httpx.Response) -> str | None:
    if not response.content:
        return None
    try:
        root = ElementTree.fromstring(response.content)
    except ElementTree.ParseError:
        return None
    return _child_text(root, "Code")


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
