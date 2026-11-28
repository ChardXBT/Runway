"""Media assets for posts: upload, resume, complete, probe (spec 02 sections 3-5).

Upload modes:

- `api` (LocalDiskStore, the `local` default): the client sends chunks to
  `PUT /api/v1/media/{id}/parts/{n}`; the store appends them under the data
  directory, outside any static mount. `local` keeps this mode even when the
  R2 settings are configured.
- `direct` (R2Store, the `hosted` mode): the client uploads parts straight to
  signed part URLs; no media bytes pass through the API. Without R2 settings
  `create` fails closed (`capability_not_ready`), never back to `api`.

Completion verifies size and SHA-256 (the local store recomputes it; R2 is
checked when the probe job copies the object), moves the asset to
`processing` and enqueues `probe_media`. The probe decides the type from the
bytes, never from the filename or the client's content type.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, BinaryIO, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runway.auth.audit import record_audit
from runway.auth.principal import MEDIA_WRITE, POSTS_READ, Principal, require
from runway.auth.problems import Problem, ProblemCode
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    ContentDraft,
    DraftMedia,
    DraftTarget,
    MediaAssetV2,
    MediaUpload,
    utcnow,
)
from runway.events.log import record_event
from runway.events.types import MEDIA_READY
from runway.jobs.queue import Job
from runway.mediastore import (
    ChecksumMismatch,
    IncompleteUpload,
    InvalidUploadRequest,
    MediaStore,
    MediaStoreError,
    MediaToolMissing,
    PartReceipt,
    ProbeError,
    ProbeResult,
    SizeMismatch,
    UploadNotFound,
    UploadPlan,
    content_type_for,
    default_poster_time_ms,
    key_extension,
    make_media_key,
    new_probe_input,
)
from runway.mediastore.probe import MediaProber, MediaTools
from runway.platforms.publisher_registry import PublisherRegistry
from runway.posts.jobs import PROBE_MEDIA_JOB, enqueue_in
from runway.posts.media import probe_media_validator, stored_probe
from runway.posts.snapshot import SnapshotSources, utc
from runway.posts.types import TargetPostType

logger = logging.getLogger(__name__)

HOSTED_DEFAULT_QUOTA_BYTES = 5 * 1000**3
UPLOAD_LIFETIME = timedelta(days=2)
# Hosted previews are short-lived signed URLs on the object store's domain;
# the API never serves media or poster bytes (spec 02 section 4).
PREVIEW_URL_TTL_SECONDS = 300
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class Prober(Protocol):
    def probe(self, path: Path) -> ProbeResult: ...

    def extract_poster(
        self, source: Path, destination: Path, *, at_ms: int = 0, max_width: int = 1280
    ) -> Path: ...


class MediaCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    filename: str = Field(min_length=1, max_length=512)
    size_bytes: int = Field(ge=1)
    content_type: str = Field(min_length=3, max_length=120)
    sha256: str = Field(min_length=64, max_length=64)


class PartIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_number: int = Field(ge=1)
    size: int = Field(ge=1)
    etag: str = Field(min_length=1, max_length=200)


class MediaComplete(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sha256: str = Field(min_length=64, max_length=64)
    # Direct (R2) uploads only: the parts the browser uploaded, with their ETags.
    parts: list[PartIn] = Field(default_factory=list, max_length=10_000)


class UploadPlanOut(BaseModel):
    mode: Literal["api", "direct"]
    part_size: int
    part_count: int
    # api mode: PUT each part's bytes to this path ({n} is the part number, from 1).
    part_path: str | None = None
    # direct mode: signed URLs by part number; they expire, ask GET /upload for fresh ones.
    part_urls: dict[int, str] | None = None


class UploadStatusOut(BaseModel):
    asset_id: int
    status: str
    mode: Literal["api", "direct"]
    part_size: int
    part_count: int
    received_parts: list[int]
    missing_parts: list[int]
    bytes_received: int
    part_urls: dict[int, str] | None = None


class MediaIssueOut(BaseModel):
    severity: str
    code: str
    message: str
    fix: str | None = None
    source: str | None = None


class MediaOut(BaseModel):
    id: int
    kind: str | None
    status: str
    rejected_reason: str | None
    original_filename: str | None
    content_type: str
    size_bytes: int
    sha256: str
    storage: str
    probe: dict[str, Any]
    has_poster: bool
    created_at: datetime
    ready_at: datetime | None
    # Which post types this file is valid for, and why not (spec 02 section 1).
    validation: dict[str, list[MediaIssueOut]]
    upload: UploadPlanOut | None = None


def _problem(code: ProblemCode, detail: str, **extra: Any) -> Problem:
    return Problem.of(code, detail, extra=extra or None)


def _store_problem(exc: MediaStoreError) -> Problem:
    if isinstance(exc, UploadNotFound):
        return _problem(ProblemCode.NOT_FOUND, "this upload is no longer available; start again")
    if isinstance(exc, InvalidUploadRequest) and "already being received" in str(exc):
        return _problem(ProblemCode.INVALID_STATE, str(exc))
    return _problem(ProblemCode.VALIDATION_FAILED, str(exc))


def default_prober(settings: Settings) -> MediaProber:
    return MediaProber(
        MediaTools(
            ffprobe=settings.ffprobe_path,
            ffmpeg=settings.ffmpeg_path,
            timeout_seconds=float(settings.media_probe_timeout_seconds),
        )
    )


def poster_root(settings: Settings) -> Path:
    """Private poster folder: outside the `/media` static mount, served only by a guarded route."""
    return settings.resolved_data_dir / "mediastore-posters"


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


@dataclass
class MediaService:
    database: Database
    settings: Settings
    stores: dict[str, MediaStore]
    registry: PublisherRegistry
    clock: Callable[[], datetime] = utcnow
    prober_factory: Callable[[], Prober] | None = None
    _prober: Prober | None = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------ helpers

    @property
    def prober(self) -> Prober:
        if self._prober is None:
            factory = self.prober_factory or (lambda: default_prober(self.settings))
            self._prober = factory()
        return self._prober

    def _store(self, storage: str) -> MediaStore:
        store = self.stores.get(storage)
        if store is None:
            raise _problem(
                ProblemCode.CAPABILITY_NOT_READY, f"the {storage} media store is not configured"
            )
        return store

    def _load(self, session: Session, principal: Principal, asset_id: int) -> MediaAssetV2:
        asset = session.get(MediaAssetV2, asset_id)
        if asset is None or asset.workspace_id != principal.workspace_id:
            raise _problem(ProblemCode.NOT_FOUND, "this media item does not exist")
        return asset

    def _upload(self, session: Session, asset: MediaAssetV2) -> tuple[MediaUpload, UploadPlan]:
        row = session.scalar(select(MediaUpload).where(MediaUpload.asset_id == asset.id))
        if row is None:
            raise _problem(ProblemCode.INVALID_STATE, "this media item has no open upload")
        return row, UploadPlan.from_dict(json.loads(row.plan_json))

    def _quota(self) -> int | None:
        return HOSTED_DEFAULT_QUOTA_BYTES if self.settings.profile == "hosted" else None

    def _plan_out(self, asset_id: int, plan: UploadPlan, store: MediaStore) -> UploadPlanOut:
        if plan.mode == "direct":
            return UploadPlanOut(
                mode="direct",
                part_size=plan.part_size,
                part_count=plan.part_count,
                part_urls=store.part_upload_urls(plan),
            )
        return UploadPlanOut(
            mode="api",
            part_size=plan.part_size,
            part_count=plan.part_count,
            part_path=f"/api/v1/media/{asset_id}/parts/{{n}}",
        )

    def view(self, asset: MediaAssetV2, upload: UploadPlanOut | None = None) -> MediaOut:
        validation: dict[str, list[MediaIssueOut]] = {}
        probe = stored_probe(asset)
        if asset.status == "ready" and probe is not None:
            for post_type in TargetPostType:
                accepts = post_type is not TargetPostType.YOUTUBE_POST or probe.kind == "image"
                if not accepts:
                    continue
                validation[post_type.value] = [
                    MediaIssueOut(
                        severity=issue.severity,
                        code=issue.code,
                        message=issue.message,
                        fix=issue.fix,
                        source=issue.source,
                    )
                    for issue in probe_media_validator(asset, post_type)
                ]
        try:
            probe_json = json.loads(asset.probe_json or "{}")
        except ValueError:
            probe_json = {}
        return MediaOut(
            id=asset.id,
            kind=asset.kind,
            status=asset.status,
            rejected_reason=asset.rejected_reason,
            original_filename=asset.original_filename,
            content_type=asset.content_type,
            size_bytes=int(asset.size_bytes),
            sha256=asset.sha256,
            storage=asset.storage,
            probe=probe_json if isinstance(probe_json, dict) else {},
            has_poster=asset.poster_key is not None,
            created_at=utc(asset.created_at) or asset.created_at,
            ready_at=utc(asset.ready_at),
            validation=validation,
            upload=upload,
        )

    # -------------------------------------------------------------- routes

    def create(self, principal: Principal, data: MediaCreate) -> MediaOut:
        require(principal, MEDIA_WRITE)
        digest = data.sha256.lower()
        if not _SHA256.match(digest):
            raise _problem(ProblemCode.VALIDATION_FAILED, "sha256 must be 64 hexadecimal digits")
        if data.size_bytes > self.settings.media_max_upload_bytes:
            raise _problem(
                ProblemCode.VALIDATION_FAILED,
                f"files are limited to {self.settings.media_max_upload_bytes} bytes",
            )
        # Hosted uploads always go direct to object storage; without the r2
        # store `_store` raises capability_not_ready instead of streaming the
        # bytes through the API. `local` keeps the api mode (invariant 8).
        storage = "r2" if self.settings.profile == "hosted" else "local"
        store = self._store(storage)
        with self.database.session() as session:
            quota = self._quota()
            if quota is not None:
                used = int(
                    session.scalar(
                        select(func.coalesce(func.sum(MediaAssetV2.size_bytes), 0)).where(
                            MediaAssetV2.workspace_id == principal.workspace_id,
                            MediaAssetV2.status != "deleted",
                        )
                    )
                    or 0
                )
                if used + data.size_bytes > quota:
                    raise _problem(
                        ProblemCode.RATE_LIMITED,
                        "this workspace's media storage is full; delete media first",
                    )
            asset = MediaAssetV2(
                workspace_id=principal.workspace_id,
                original_filename=data.filename,
                content_type=data.content_type.split(";", 1)[0].strip().lower(),
                size_bytes=data.size_bytes,
                sha256=digest,
                storage=storage,
                # Placeholder until the id exists; replaced below in the same transaction.
                storage_key=f"pending/{digest}/{self.clock().timestamp()}",
                status="uploading",
                created_by=f"{principal.kind}:{principal.id}",
            )
            session.add(asset)
            session.flush()
            asset.storage_key = make_media_key(
                principal.workspace_id, asset.id, digest, key_extension(asset.content_type)
            )
            try:
                plan = store.begin_upload(
                    asset.storage_key,
                    data.size_bytes,
                    asset.content_type,
                    sha256=digest,
                    workspace_id=principal.workspace_id,
                )
            except MediaStoreError as exc:
                raise _store_problem(exc) from None
            session.add(
                MediaUpload(
                    workspace_id=principal.workspace_id,
                    asset_id=asset.id,
                    upload_id=plan.upload_id,
                    part_size=plan.part_size,
                    plan_json=json.dumps(plan.to_dict()),
                    expires_at=self.clock() + UPLOAD_LIFETIME,
                )
            )
            session.flush()
            return self.view(asset, self._plan_out(asset.id, plan, store))

    def get(self, principal: Principal, asset_id: int) -> MediaOut:
        require(principal, POSTS_READ)
        with self.database.session() as session:
            return self.view(self._load(session, principal, asset_id))

    def receive_part(
        self, principal: Principal, asset_id: int, part_number: int, data: BinaryIO | bytes
    ) -> UploadStatusOut:
        require(principal, MEDIA_WRITE)
        with self.database.session() as session:
            asset = self._load(session, principal, asset_id)
            if asset.status != "uploading":
                raise _problem(ProblemCode.INVALID_STATE, f"this media item is {asset.status}")
            _row, plan = self._upload(session, asset)
        if plan.mode != "api":
            raise _problem(
                ProblemCode.INVALID_STATE, "this upload goes straight to storage, not the API"
            )
        store = self._store(asset.storage)
        try:
            store.receive_part(plan, part_number, data)
        except MediaStoreError as exc:
            raise _store_problem(exc) from None
        return self.upload_status(principal, asset_id)

    def upload_status(self, principal: Principal, asset_id: int) -> UploadStatusOut:
        require(principal, MEDIA_WRITE)
        with self.database.session() as session:
            asset = self._load(session, principal, asset_id)
            _row, plan = self._upload(session, asset)
            status = asset.status
        store = self._store(asset.storage)
        if status != "uploading":
            received = list(plan.part_numbers())
            return UploadStatusOut(
                asset_id=asset_id,
                status=status,
                mode=plan.mode,
                part_size=plan.part_size,
                part_count=plan.part_count,
                received_parts=received,
                missing_parts=[],
                bytes_received=plan.size,
            )
        try:
            progress = store.upload_progress(plan)
        except MediaStoreError as exc:
            raise _store_problem(exc) from None
        missing = list(progress.missing_part_numbers)
        return UploadStatusOut(
            asset_id=asset_id,
            status=status,
            mode=plan.mode,
            part_size=plan.part_size,
            part_count=plan.part_count,
            received_parts=[part.part_number for part in progress.parts],
            missing_parts=missing,
            bytes_received=progress.bytes_received,
            part_urls=store.part_upload_urls(plan, missing) if plan.mode == "direct" else None,
        )

    def complete(self, principal: Principal, asset_id: int, data: MediaComplete) -> MediaOut:
        require(principal, MEDIA_WRITE)
        with self.database.session() as session:
            asset = self._load(session, principal, asset_id)
            if asset.status != "uploading":
                if asset.status in {"processing", "ready"}:
                    return self.view(asset)  # completing twice is harmless
                raise _problem(ProblemCode.INVALID_STATE, f"this media item is {asset.status}")
            if data.sha256.lower() != asset.sha256:
                raise _problem(
                    ProblemCode.VALIDATION_FAILED,
                    "the SHA-256 differs from the one declared when the upload started",
                )
            row, plan = self._upload(session, asset)
            store = self._store(asset.storage)
            receipts = [
                PartReceipt(part_number=part.part_number, size=part.size, etag=part.etag)
                for part in data.parts
            ]
            try:
                store.complete_upload(plan, receipts)
            except (ChecksumMismatch, SizeMismatch) as exc:
                asset.status = "rejected"
                asset.rejected_reason = str(exc)
                row.completed_at = self.clock()
                return self.view(asset)
            except IncompleteUpload as exc:
                raise _problem(ProblemCode.VALIDATION_FAILED, str(exc)) from None
            except MediaStoreError as exc:
                raise _store_problem(exc) from None
            now = self.clock()
            row.completed_at = now
            asset.status = "processing"
            enqueue_in(
                session,
                PROBE_MEDIA_JOB,
                idempotency_key=f"probe_media:{asset.id}",
                run_at=now,
                workspace_id=asset.workspace_id,
                payload={"asset_id": asset.id},
            )
            return self.view(asset)

    def delete(self, principal: Principal, asset_id: int) -> None:
        require(principal, MEDIA_WRITE)
        with self.database.session() as session:
            asset = self._load(session, principal, asset_id)
            if asset.status == "deleted":
                return
            used = session.scalar(select(DraftMedia.id).where(DraftMedia.asset_id == asset.id))
            if used is not None:
                raise _problem(
                    ProblemCode.INVALID_STATE,
                    "this media item is used by a post; remove it from the post first",
                )
            store = self._store(asset.storage)
            upload = session.scalar(select(MediaUpload).where(MediaUpload.asset_id == asset.id))
            try:
                if asset.status == "uploading" and upload is not None:
                    store.abort_upload(UploadPlan.from_dict(json.loads(upload.plan_json)))
                else:
                    store.delete(asset.storage_key, workspace_id=asset.workspace_id)
            except MediaStoreError:
                logger.warning("media %s: stored bytes were already gone", asset.id)
            self._remove_poster(asset)
            asset.status = "deleted"
            asset.poster_key = None
            record_audit(
                session,
                principal,
                "media.delete",
                target_type="media",
                target_id=asset.id,
                detail={"storage": asset.storage},
            )

    def poster_path(self, principal: Principal, asset_id: int) -> Path:
        require(principal, POSTS_READ)
        if self.settings.profile != "local":
            # Hosted previews use short-lived signed URLs, never the API origin.
            raise _problem(ProblemCode.NOT_FOUND, "previews are not served by this API")
        with self.database.session() as session:
            asset = self._load(session, principal, asset_id)
            if asset.poster_key is None or asset.status == "deleted":
                raise _problem(ProblemCode.NOT_FOUND, "this media item has no preview")
            path = self._poster_file(asset.workspace_id, asset.id)
        if not path.is_file():
            raise _problem(ProblemCode.NOT_FOUND, "this media item has no preview")
        return path

    def poster_url(self, principal: Principal, asset_id: int) -> str:
        """Hosted preview: a short-lived signed URL for the poster object.

        The poster frame lives in the same object store as the media in
        `hosted`, so the API only ever hands out a signed URL to it.
        """
        require(principal, POSTS_READ)
        if self.settings.profile == "local":
            raise _problem(ProblemCode.NOT_FOUND, "previews are files in this profile")
        with self.database.session() as session:
            asset = self._load(session, principal, asset_id)
            poster_key = asset.poster_key
            if poster_key is None or asset.status == "deleted":
                raise _problem(ProblemCode.NOT_FOUND, "this media item has no preview")
            storage, workspace_id = asset.storage, asset.workspace_id
        url = self._store(storage).signed_preview_url(
            poster_key,
            PREVIEW_URL_TTL_SECONDS,
            workspace_id=workspace_id,
            content_type="image/jpeg",
        )
        if url is None:
            raise _problem(ProblemCode.NOT_FOUND, "this media item has no preview")
        return url

    # ------------------------------------------------------------ probing

    def _poster_file(self, workspace_id: int, asset_id: int) -> Path:
        return poster_root(self.settings) / f"w{int(workspace_id)}" / f"{int(asset_id)}.jpg"

    def _remove_poster(self, asset: MediaAssetV2) -> None:
        if asset.storage != "local" and asset.poster_key is not None:
            # Object-store poster: the key names a stored object, not a file.
            store = self.stores.get(asset.storage)
            try:
                if store is not None:
                    store.delete(asset.poster_key, workspace_id=asset.workspace_id)
            except MediaStoreError:
                logger.warning("media %s: poster object was already gone", asset.id)
            return
        self._poster_file(asset.workspace_id, asset.id).unlink(missing_ok=True)

    def _store_poster(
        self,
        store: MediaStore,
        *,
        asset_id: int,
        workspace_id: int,
        storage: str,
        poster: Path,
    ) -> str:
        """Persist the extracted frame; return the `poster_key` to record.

        An R2 asset gets an object under the same ``w/{workspace}/{asset}``
        tree; a local asset keeps the poster as a file under the private
        poster root with the historical ``poster:{id}`` marker.
        """
        if storage != "local":
            digest = _file_sha256(poster)
            key = make_media_key(workspace_id, f"{asset_id}-poster", digest, "jpg")
            store.put_object(
                key,
                poster.read_bytes(),
                workspace_id=workspace_id,
                content_type="image/jpeg",
            )
            return key
        final = self._poster_file(workspace_id, asset_id)
        final.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(poster), final)
        return f"poster:{asset_id}"

    def _scratch(self, store: MediaStore) -> Path:
        scratch = getattr(store, "scratch_dir", None)
        if isinstance(scratch, Path):
            scratch.mkdir(parents=True, exist_ok=True)
            return scratch
        directory = self.settings.resolved_data_dir / "runtime" / "probe"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def run_probe_job(self, job: Job) -> None:
        """`probe_media`: copy, verify, probe, extract a poster; `ready` or `rejected`."""
        asset_id = int(job.payload.get("asset_id") or 0)
        with self.database.session() as session:
            asset = session.get(MediaAssetV2, asset_id)
            if asset is None or asset.workspace_id != job.workspace_id:
                return
            if asset.status != "processing":
                return
            key, workspace_id = asset.storage_key, asset.workspace_id
            sha256, size, storage = asset.sha256, int(asset.size_bytes), asset.storage
        store = self._store(storage)
        destination = new_probe_input(self._scratch(store))
        poster_tmp = Path(tempfile.mkdtemp(prefix="poster-", dir=self._scratch(store)))
        probe: ProbeResult | None = None
        reason: str | None = None
        poster_key: str | None = None
        try:
            try:
                store.copy_to_path(
                    key, destination, workspace_id=workspace_id, sha256=sha256, size=size
                )
            except (ChecksumMismatch, SizeMismatch):
                reason = "the stored bytes do not match the upload's size and SHA-256"
                try:
                    store.delete(key, workspace_id=workspace_id)
                except MediaStoreError:
                    logger.warning("media %s: could not delete mismatched bytes", asset_id)
            if reason is None:
                try:
                    probe = self.prober.probe(destination)
                except MediaToolMissing:
                    raise  # retried by the queue; the asset stays processing
                except ProbeError as exc:
                    reason = str(exc)
            if probe is not None and probe.kind not in {"video", "image"}:
                reason = "this file is not a video or an image"
            if probe is not None and reason is None:
                poster = poster_tmp / "poster.jpg"
                try:
                    self.prober.extract_poster(
                        destination, poster, at_ms=default_poster_time_ms(probe)
                    )
                    # A store failure propagates so the job retries instead of
                    # marking the asset ready without a preview.
                    poster_key = self._store_poster(
                        store,
                        asset_id=asset_id,
                        workspace_id=workspace_id,
                        storage=storage,
                        poster=poster,
                    )
                except (ProbeError, OSError) as exc:
                    logger.info("media %s: no poster (%s)", asset_id, type(exc).__name__)
        finally:
            destination.unlink(missing_ok=True)
            shutil.rmtree(poster_tmp, ignore_errors=True)
        with self.database.session() as session:
            asset = session.get(MediaAssetV2, asset_id)
            if asset is None or asset.status != "processing":
                return
            now = self.clock()
            if reason is not None or probe is None:
                asset.status = "rejected"
                asset.rejected_reason = (reason or "the file could not be checked")[:1000]
            else:
                asset.status = "ready"
                asset.kind = probe.kind
                asset.content_type = content_type_for(probe) or asset.content_type
                asset.probe_json = json.dumps(probe.to_dict(), sort_keys=True)
                asset.ready_at = now
                asset.poster_key = poster_key
            session.flush()
            record_event(
                session,
                asset.workspace_id,
                MEDIA_READY,
                "media",
                asset.id,
                {"asset_id": asset.id, "status": asset.status, "kind": asset.kind},
            )
            self._revalidate_posts(session, asset)

    def _revalidate_posts(self, session: Session, asset: MediaAssetV2) -> None:
        """Targets waiting on this asset move draft -> ready (or stay draft with the reason)."""
        from runway.posts.service import revalidate

        sources = SnapshotSources(
            settings=self.settings,
            registry=self.registry,
            clock=self.clock,
            media_validator=probe_media_validator,
            media_stores=self.stores,
        )
        post_ids = session.scalars(
            select(DraftMedia.draft_id).where(DraftMedia.asset_id == asset.id)
        ).all()
        for post_id in post_ids:
            post = session.get(ContentDraft, post_id)
            if post is None:
                continue
            for target in session.scalars(
                select(DraftTarget).where(DraftTarget.draft_id == post.id)
            ):
                revalidate(session, sources, target, post)

    # -------------------------------------------------------- imports

    def import_file(
        self,
        session: Session,
        *,
        workspace_id: int,
        source: Path,
        content_type: str,
        filename: str,
        created_by: str,
    ) -> MediaAssetV2:
        """Copy a file the server already holds (a proposal image) into the local store.

        The asset goes through the same probe as an upload; nothing trusts its name.
        """
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
        sha256 = digest.hexdigest()
        size = source.stat().st_size
        store = self._store("local")
        asset = MediaAssetV2(
            workspace_id=workspace_id,
            original_filename=filename[:512],
            content_type=content_type,
            size_bytes=size,
            sha256=sha256,
            storage="local",
            storage_key=f"pending/{sha256}/{self.clock().timestamp()}",
            status="uploading",
            created_by=created_by,
        )
        session.add(asset)
        session.flush()
        asset.storage_key = make_media_key(
            workspace_id, asset.id, sha256, key_extension(content_type)
        )
        try:
            plan = store.begin_upload(
                asset.storage_key, size, content_type, sha256=sha256, workspace_id=workspace_id
            )
            for number in plan.part_numbers():
                with source.open("rb") as handle:
                    handle.seek(plan.part_offset(number))
                    store.receive_part(plan, number, handle.read(plan.part_length(number)))
            store.complete_upload(plan)
        except MediaStoreError as exc:
            raise _store_problem(exc) from None
        now = self.clock()
        session.add(
            MediaUpload(
                workspace_id=workspace_id,
                asset_id=asset.id,
                upload_id=plan.upload_id,
                part_size=plan.part_size,
                plan_json=json.dumps(plan.to_dict()),
                expires_at=now,
                completed_at=now,
            )
        )
        asset.status = "processing"
        enqueue_in(
            session,
            PROBE_MEDIA_JOB,
            idempotency_key=f"probe_media:{asset.id}",
            run_at=now,
            workspace_id=workspace_id,
            payload={"asset_id": asset.id},
        )
        session.flush()
        return asset
