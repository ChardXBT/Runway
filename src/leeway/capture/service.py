from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel
from sqlalchemy import desc, or_, select
from sqlalchemy.orm import Session

from leeway.capture.adapter import YouTubeCommunityPostsAdapterV1, fixture_dom_path
from leeway.capture.schemas import ExtractedPost, ExtractionDiagnostic, ImageReference
from leeway.config import Settings
from leeway.db.base import Database
from leeway.db.models import CaptureRun, MediaAsset, Post, PostMedia, RawPostRecord, utcnow
from leeway.db.repositories import audit, get_channel
from leeway.domain.enums import CaptureMode, MediaKind, PostType, RightsStatus, RunStatus
from leeway.media.service import (
    content_addressed_copy,
    ensure_fixture_images,
    inspect_image,
)


class CaptureResult(BaseModel):
    run_id: int | None
    status: str
    posts_seen: int
    posts_created: int
    posts_updated: int
    media_downloaded: int
    diagnostics: int = 0
    cursor: int = 0


class CaptureService:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self.adapter = YouTubeCommunityPostsAdapterV1()

    def run_fixture(
        self,
        *,
        resume: bool = True,
        max_posts: int | None = None,
        dry_run: bool = False,
        save_all_snapshots: bool = False,
    ) -> CaptureResult:
        batch = self.adapter.extract_file(fixture_dom_path())
        if dry_run:
            unique = {post.stable_key() for post in batch.posts if post.stable_key()}
            return CaptureResult(
                run_id=None,
                status="dry_run",
                posts_seen=len(unique),
                posts_created=0,
                posts_updated=0,
                media_downloaded=0,
                diagnostics=len(batch.diagnostics),
                cursor=len(batch.posts),
            )

        fixture_assets = ensure_fixture_images(self.settings)
        channel_url = "fixture://qlob/community"
        with self.database.session() as session:
            run = self._get_or_create_run(session, CaptureMode.FIXTURE, channel_url, resume=resume)
            cursor = self._cursor(run)
            start = int(cursor.get("fixture_index", 0))
            limit = (
                len(batch.posts) if max_posts is None else min(len(batch.posts), start + max_posts)
            )
            self._record_diagnostics(
                session,
                run,
                batch.diagnostics,
                save_all_snapshots=save_all_snapshots,
            )
            for index in range(start, limit):
                self._process_post(session, run, batch.posts[index], fixture_assets)
                cursor["fixture_index"] = index + 1
                cursor["seen_keys"] = sorted(
                    set(cursor.get("seen_keys", [])) | {batch.posts[index].stable_key()}
                )
                run.posts_seen = len([key for key in cursor["seen_keys"] if key])
                run.last_cursor_json = json.dumps(cursor, sort_keys=True)
                if run.posts_seen and run.posts_seen % self.settings.capture_checkpoint_every == 0:
                    session.flush()
                    session.commit()
            complete = limit >= len(batch.posts)
            run.status = RunStatus.COMPLETED if complete else RunStatus.PAUSED
            run.completed_at = utcnow() if complete else None
            run.last_cursor_json = json.dumps(cursor, sort_keys=True)
            audit(
                session,
                "capture_completed" if complete else "capture_paused",
                "capture_run",
                run.id,
                {"cursor": limit, "fixture": True},
            )
            session.flush()
            return self._result(run, len(batch.diagnostics))

    def append_records(
        self,
        records: Iterable[ExtractedPost],
        *,
        mode: CaptureMode,
        channel_url: str,
        resume: bool = True,
        finalize: bool = False,
    ) -> CaptureResult:
        """Append one safe browser checkpoint; used only by explicit live capture."""
        with self.database.session() as session:
            run = self._get_or_create_run(session, mode, channel_url, resume=resume)
            cursor = self._cursor(run)
            seen = set(cursor.get("seen_keys", []))
            for record in records:
                key = record.stable_key()
                if key and key in seen:
                    continue
                self._process_post(session, run, record, fixture_assets=None)
                if key:
                    seen.add(key)
                run.posts_seen = len(seen)
                if run.posts_seen % self.settings.capture_checkpoint_every == 0:
                    session.flush()
                    session.commit()
            cursor["seen_keys"] = sorted(seen)
            run.last_cursor_json = json.dumps(cursor, sort_keys=True)
            run.status = RunStatus.COMPLETED if finalize else RunStatus.PAUSED
            run.completed_at = utcnow() if finalize else None
            session.flush()
            return self._result(run, 0)

    def latest_status(self) -> dict[str, object] | None:
        with self.database.session() as session:
            run = session.scalar(select(CaptureRun).order_by(desc(CaptureRun.started_at)).limit(1))
            if run is None:
                return None
            return self._result(run, 0).model_dump()

    def _get_or_create_run(
        self,
        session: Session,
        mode: CaptureMode,
        channel_url: str,
        *,
        resume: bool,
    ) -> CaptureRun:
        channel = get_channel(session, self.settings.channel_handle)
        run: CaptureRun | None = None
        if resume:
            run = session.scalar(
                select(CaptureRun)
                .where(
                    CaptureRun.channel_id == channel.id,
                    CaptureRun.mode == mode.value,
                    CaptureRun.status.in_(
                        [RunStatus.RUNNING.value, RunStatus.PAUSED.value, RunStatus.FAILED.value]
                    ),
                )
                .order_by(desc(CaptureRun.started_at))
                .limit(1)
            )
        if run is None:
            run = CaptureRun(
                channel_id=channel.id,
                mode=mode.value,
                channel_url=channel_url,
                status=RunStatus.RUNNING.value,
                selector_adapter_version=self.adapter.version,
                last_cursor_json="{}",
            )
            session.add(run)
            session.flush()
            audit(session, "capture_started", "capture_run", run.id, {"mode": mode.value})
        else:
            run.status = RunStatus.RUNNING.value
            run.error_summary = None
        return run

    @staticmethod
    def _cursor(run: CaptureRun) -> dict[str, Any]:
        try:
            value = json.loads(run.last_cursor_json or "{}")
            return value if isinstance(value, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _record_diagnostics(
        self,
        session: Session,
        run: CaptureRun,
        diagnostics: list[ExtractionDiagnostic],
        *,
        save_all_snapshots: bool,
    ) -> None:
        for index, diagnostic in enumerate(diagnostics):
            key = f"diagnostic-{index}-{diagnostic.code}"
            exists = session.scalar(
                select(RawPostRecord.id).where(
                    RawPostRecord.capture_run_id == run.id,
                    RawPostRecord.record_key == key,
                )
            )
            if exists:
                continue
            snapshot_path: str | None = None
            if save_all_snapshots and diagnostic.snippet:
                path = self.settings.resolved_data_dir / "snapshots" / f"run-{run.id}-{key}.html"
                path.write_text(diagnostic.snippet, encoding="utf-8")
                snapshot_path = str(path.relative_to(self.settings.resolved_data_dir).as_posix())
            session.add(
                RawPostRecord(
                    capture_run_id=run.id,
                    record_key=key,
                    raw_json=diagnostic.model_dump_json(),
                    raw_dom_snapshot_path=snapshot_path,
                )
            )
            audit(
                session,
                "capture_diagnostic",
                "capture_run",
                run.id,
                diagnostic.model_dump(),
            )

    def _process_post(
        self,
        session: Session,
        run: CaptureRun,
        record: ExtractedPost,
        fixture_assets: dict[str, Path] | None,
    ) -> None:
        key = (
            record.stable_key()
            or hashlib.sha256(record.model_dump_json().encode("utf-8")).hexdigest()
        )
        existing_raw = session.scalar(
            select(RawPostRecord.id).where(
                RawPostRecord.capture_run_id == run.id,
                RawPostRecord.record_key == key,
            )
        )
        if existing_raw is None:
            session.add(
                RawPostRecord(
                    capture_run_id=run.id,
                    record_key=key,
                    external_post_id=record.external_post_id,
                    permalink=record.permalink,
                    raw_json=record.model_dump_json(),
                )
            )

        conditions = []
        if record.external_post_id:
            conditions.append(Post.external_post_id == record.external_post_id)
        if record.permalink:
            conditions.append(Post.permalink == record.permalink)
        post = session.scalar(
            select(Post).where(Post.channel_id == run.channel_id, or_(*conditions)).limit(1)
        )
        creating = post is None
        if post is None:
            post = Post(
                channel_id=run.channel_id,
                external_post_id=record.external_post_id,
                permalink=record.permalink,
                post_type=record.post_type.value,
                caption=record.caption,
                displayed_date_text=record.displayed_date_text,
                published_at=record.published_at,
                date_precision=record.date_precision.value,
                like_count=record.like_count,
                comment_count=record.comment_count,
                raw_engagement_json=json.dumps(
                    {"likes": record.raw_like_text, "comments": record.raw_comment_text},
                    sort_keys=True,
                ),
                is_published=True,
                is_training_eligible=False,
                source_capture_run_id=run.id,
            )
            session.add(post)
            session.flush()
            run.posts_created += 1
        else:
            post.caption = record.caption
            post.displayed_date_text = record.displayed_date_text
            post.published_at = record.published_at
            post.date_precision = record.date_precision.value
            post.like_count = record.like_count
            post.comment_count = record.comment_count
            post.post_type = record.post_type.value
            post.raw_engagement_json = json.dumps(
                {"likes": record.raw_like_text, "comments": record.raw_comment_text}, sort_keys=True
            )
            run.posts_updated += 1

        linked_count = 0
        for position, reference in enumerate(record.images):
            try:
                asset, downloaded = self._ingest_reference(
                    session, reference, MediaKind.HISTORICAL, fixture_assets
                )
                if downloaded:
                    run.media_downloaded += 1
            except Exception as exc:
                audit(
                    session,
                    "media_download_error",
                    "post",
                    post.id,
                    {"url": reference.url, "error": f"{type(exc).__name__}: {exc}"},
                )
                continue
            link = session.scalar(
                select(PostMedia).where(
                    PostMedia.post_id == post.id,
                    PostMedia.position == position,
                )
            )
            if link is None:
                session.add(PostMedia(post_id=post.id, media_asset_id=asset.id, position=position))
            else:
                link.media_asset_id = asset.id
            linked_count += 1

        post.is_training_eligible = bool(
            record.post_type in {PostType.IMAGE, PostType.MULTI_IMAGE}
            and record.caption
            and linked_count
        )
        audit(
            session,
            "post_created" if creating else "post_updated",
            "post",
            post.id,
            {"capture_run_id": run.id, "training_eligible": post.is_training_eligible},
        )

    def _ingest_reference(
        self,
        session: Session,
        reference: ImageReference,
        kind: MediaKind,
        fixture_assets: dict[str, Path] | None,
    ) -> tuple[MediaAsset, bool]:
        source = self._resolve_reference(reference, fixture_assets)
        features = inspect_image(source)
        existing = session.scalar(
            select(MediaAsset).where(MediaAsset.sha256 == features.sha256).limit(1)
        )
        if existing is not None:
            if existing.embedding_model != features.embedding_model:
                existing.width = features.width
                existing.height = features.height
                existing.file_size = features.file_size
                existing.perceptual_hash = features.perceptual_hash
                existing.crop_resistant_hash = features.crop_resistant_hash
                existing.embedding_model = features.embedding_model
                existing.embedding_vector = features.embedding_bytes()
                existing.blur_score = features.blur_score
                existing.quality_metrics_json = json.dumps(features.quality_metrics, sort_keys=True)
            return existing, False
        _destination, relative = content_addressed_copy(source, self.settings, kind.value, features)
        parsed = urlparse(reference.url)
        asset = MediaAsset(
            kind=kind.value,
            local_path=relative,
            original_url=reference.url,
            source_page_url=None,
            source_domain=parsed.netloc or "fixture.local",
            original_filename=Path(parsed.path).name or f"{features.sha256}.jpg",
            mime_type=features.mime_type,
            width=features.width,
            height=features.height,
            file_size=features.file_size,
            sha256=features.sha256,
            perceptual_hash=features.perceptual_hash,
            crop_resistant_hash=features.crop_resistant_hash,
            embedding_model=features.embedding_model,
            embedding_vector=features.embedding_bytes(),
            blur_score=features.blur_score,
            quality_metrics_json=json.dumps(features.quality_metrics, sort_keys=True),
            downloaded_at=datetime.now(UTC),
            rights_status=(
                RightsStatus.CREATOR_OWNED.value
                if reference.url.startswith("fixture://")
                else RightsStatus.UNKNOWN.value
            ),
        )
        session.add(asset)
        session.flush()
        return asset, True

    def _resolve_reference(
        self, reference: ImageReference, fixture_assets: dict[str, Path] | None
    ) -> Path:
        if reference.url.startswith("fixture://"):
            if fixture_assets is None:
                fixture_assets = ensure_fixture_images(self.settings)
            name = reference.url.removeprefix("fixture://")
            if name not in fixture_assets:
                raise FileNotFoundError(f"unknown fixture asset {name}")
            return fixture_assets[name]

        parsed = urlparse(reference.url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("only HTTPS/HTTP and fixture image URLs are supported")
        with httpx.Client(timeout=httpx.Timeout(20.0), follow_redirects=True) as client:
            response = client.get(reference.url)
            response.raise_for_status()
            content_type = response.headers.get("content-type", "").split(";")[0]
            if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                raise ValueError(f"unsupported MIME type {content_type or 'unknown'}")
            if len(response.content) > self.settings.maximum_image_bytes:
                raise ValueError("image exceeds configured maximum size")
            digest = hashlib.sha256(response.content).hexdigest()
            extension = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}[
                content_type
            ]
            target = self.settings.resolved_data_dir / "raw" / "downloads" / f"{digest}{extension}"
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                target.write_bytes(response.content)
            return target

    @staticmethod
    def _result(run: CaptureRun, diagnostics: int) -> CaptureResult:
        cursor = CaptureService._cursor(run)
        return CaptureResult(
            run_id=run.id,
            status=run.status,
            posts_seen=run.posts_seen,
            posts_created=run.posts_created,
            posts_updated=run.posts_updated,
            media_downloaded=run.media_downloaded,
            diagnostics=diagnostics,
            cursor=int(cursor.get("fixture_index", run.posts_seen)),
        )
