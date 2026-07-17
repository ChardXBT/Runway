from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel
from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from leeway.capture.adapter import YouTubeCommunityPostsAdapterV1, fixture_dom_path
from leeway.capture.schemas import (
    BrowserDomSnapshot,
    ExtractedPost,
    ExtractionDiagnostic,
    ImageReference,
)
from leeway.config import Settings
from leeway.db.base import Database
from leeway.db.models import (
    AuditEvent,
    CaptureRun,
    MediaAsset,
    Post,
    PostMedia,
    RawPostRecord,
    utcnow,
)
from leeway.db.repositories import audit, get_channel
from leeway.domain.enums import (
    CaptureMode,
    DatePrecision,
    MediaKind,
    PostType,
    RightsStatus,
    RunStatus,
)
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
        diagnostics: list[ExtractionDiagnostic] | None = None,
        cursor_updates: dict[str, object] | None = None,
    ) -> CaptureResult:
        """Append one safe browser checkpoint; used only by explicit live capture."""
        with self.database.session() as session:
            run = self._get_or_create_run(session, mode, channel_url, resume=resume)
            cursor = self._cursor(run)
            seen = set(cursor.get("seen_keys", []))
            self._record_diagnostics(
                session,
                run,
                diagnostics or [],
                save_all_snapshots=False,
            )
            for record in records:
                key = record.stable_key()
                if key and key in seen:
                    continue
                record_complete = self._process_post(session, run, record, fixture_assets=None)
                if key and record_complete:
                    seen.add(key)
                run.posts_seen = len(seen)
                if run.posts_seen % self.settings.capture_checkpoint_every == 0:
                    session.flush()
                    session.commit()
            cursor["seen_keys"] = sorted(seen)
            if cursor_updates:
                cursor.update(cursor_updates)
            run.last_cursor_json = json.dumps(cursor, sort_keys=True)
            run.status = RunStatus.COMPLETED if finalize else RunStatus.PAUSED
            run.completed_at = utcnow() if finalize else None
            session.flush()
            return self._result(run, len(diagnostics or []))

    def append_dom_checkpoint(
        self,
        snapshots: list[BrowserDomSnapshot],
        *,
        channel_url: str,
        surface_card_count: int,
        surface_tail_key: str,
        finalize: bool = False,
        expected_total: int | None = None,
    ) -> CaptureResult:
        """Ingest a bounded checkpoint supplied by an explicit local browser agent."""
        parsed_url = urlparse(channel_url)
        if parsed_url.scheme != "https" or (parsed_url.hostname or "").lower() not in {
            "youtube.com",
            "www.youtube.com",
            "m.youtube.com",
        }:
            raise ValueError("browser checkpoint requires an HTTPS youtube.com channel URL")
        if not snapshots:
            raise ValueError("browser checkpoint requires at least one DOM snapshot")

        records: list[ExtractedPost] = []
        diagnostics: list[ExtractionDiagnostic] = []
        for index, snapshot in enumerate(snapshots):
            batch = self.adapter.extract_html(
                snapshot.html,
                base_url=channel_url,
                observed_at=snapshot.observed_at,
            )
            diagnostics.extend(batch.diagnostics)
            if len(batch.posts) != 1:
                if not batch.diagnostics:
                    diagnostics.append(
                        ExtractionDiagnostic(
                            code="browser_checkpoint_parse_count",
                            message=(
                                f"Browser checkpoint card {index} produced "
                                f"{len(batch.posts)} posts instead of one."
                            ),
                            snippet=snapshot.html[:500],
                        )
                    )
                continue
            post = batch.posts[0]
            post.raw.update(
                {
                    "capture_transport": "browser_agent_checkpoint",
                    "surface_card_count": surface_card_count,
                }
            )
            post.raw_dom_snapshot_path = self.save_post_snapshot(post, snapshot.html)
            records.append(post)

        result = self.append_records(
            records,
            mode=CaptureMode.MANAGED_BROWSER,
            channel_url=channel_url,
            resume=True,
            finalize=False,
            diagnostics=diagnostics,
            cursor_updates={
                "capture_transport": "browser_agent_checkpoint",
                "surface_card_count": surface_card_count,
                "surface_tail_key": surface_tail_key,
                "last_checkpoint_at": datetime.now(UTC).isoformat(),
            },
        )
        if not finalize:
            return result
        if diagnostics:
            raise ValueError("cannot finalize a browser checkpoint with parse diagnostics")
        if expected_total is None:
            raise ValueError("final browser checkpoint requires expected_total")
        if surface_card_count != expected_total:
            raise ValueError(
                "cannot finalize: expected_total must equal the verified surface card count"
            )
        if not records or records[-1].stable_key() != surface_tail_key:
            raise ValueError("cannot finalize: final checkpoint does not end at the surface tail")
        if result.posts_seen != expected_total:
            raise ValueError(
                f"cannot finalize: stored {result.posts_seen} of {expected_total} expected posts"
            )
        return self.append_records(
            [],
            mode=CaptureMode.MANAGED_BROWSER,
            channel_url=channel_url,
            resume=True,
            finalize=True,
            cursor_updates={
                "capture_transport": "browser_agent_checkpoint",
                "surface_card_count": surface_card_count,
                "surface_tail_key": surface_tail_key,
                "completion_reason": "browser_agent_verified_stable_surface",
                "capture_finished_at": datetime.now(UTC).isoformat(),
            },
        )

    def save_post_snapshot(self, post: ExtractedPost, html: str) -> str:
        key = post.stable_key() or f"unkeyed-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
        safe_key = re.sub(r"[^A-Za-z0-9_.-]", "_", key)[:120]
        path = self.settings.resolved_data_dir / "captures" / "posts" / f"{safe_key}.html"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text(html, encoding="utf-8")
        return path.relative_to(self.settings.resolved_data_dir).as_posix()

    def active_seen_keys(self, mode: CaptureMode) -> set[str]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            statuses = [
                RunStatus.RUNNING.value,
                RunStatus.PAUSED.value,
                RunStatus.FAILED.value,
            ]
            if mode in {CaptureMode.MANAGED_BROWSER, CaptureMode.CDP}:
                statuses.append(RunStatus.COMPLETED.value)
            run = session.scalar(
                select(CaptureRun)
                .where(
                    CaptureRun.channel_id == channel.id,
                    CaptureRun.mode == mode.value,
                    CaptureRun.status.in_(statuses),
                )
                .order_by(desc(CaptureRun.started_at))
                .limit(1)
            )
            if run is None:
                return set()
            return {
                str(key)
                for key in self._cursor(run).get("seen_keys", [])
                if isinstance(key, str) and key
            }

    def latest_status(self) -> dict[str, object] | None:
        with self.database.session() as session:
            run = session.scalar(select(CaptureRun).order_by(desc(CaptureRun.started_at)).limit(1))
            if run is None:
                return None
            return self._result(run, 0).model_dump()

    def reparse_snapshots(
        self,
        *,
        run_id: int | None = None,
        dry_run: bool = False,
    ) -> dict[str, object]:
        """Rebuild normalized metadata from immutable per-post DOM snapshots."""
        with self.database.session() as session:
            run = (
                session.get(CaptureRun, run_id)
                if run_id is not None
                else session.scalar(
                    select(CaptureRun).order_by(desc(CaptureRun.started_at)).limit(1)
                )
            )
            if run is None:
                raise LookupError("no capture run is available for snapshot reparse")
            raw_records = session.scalars(
                select(RawPostRecord)
                .where(
                    RawPostRecord.capture_run_id == run.id,
                    RawPostRecord.raw_dom_snapshot_path.is_not(None),
                )
                .order_by(RawPostRecord.id)
            ).all()
            scanned = 0
            reparsed = 0
            errors: list[dict[str, object]] = []
            data_root = self.settings.resolved_data_dir.resolve()
            for raw_record in raw_records:
                scanned += 1
                relative = raw_record.raw_dom_snapshot_path
                if not relative:
                    continue
                snapshot = (data_root / relative).resolve()
                if not snapshot.is_relative_to(data_root) or not snapshot.is_file():
                    errors.append(
                        {
                            "record_key": raw_record.record_key,
                            "error": "snapshot_missing_or_outside_data_root",
                        }
                    )
                    continue
                observed_at = raw_record.captured_at
                if observed_at.tzinfo is None:
                    observed_at = observed_at.replace(tzinfo=UTC)
                batch = self.adapter.extract_html(
                    snapshot.read_text(encoding="utf-8"),
                    observed_at=observed_at,
                )
                if len(batch.posts) != 1 or batch.diagnostics:
                    errors.append(
                        {
                            "record_key": raw_record.record_key,
                            "error": "snapshot_parse_incomplete",
                            "post_count": len(batch.posts),
                            "diagnostics": [
                                diagnostic.model_dump() for diagnostic in batch.diagnostics
                            ],
                        }
                    )
                    continue
                extracted = batch.posts[0]
                if extracted.stable_key() != raw_record.record_key:
                    errors.append(
                        {
                            "record_key": raw_record.record_key,
                            "error": "snapshot_key_mismatch",
                            "extracted_key": extracted.stable_key(),
                        }
                    )
                    continue
                post = session.scalar(
                    select(Post)
                    .where(
                        Post.channel_id == run.channel_id,
                        or_(
                            Post.external_post_id == extracted.external_post_id,
                            Post.permalink == extracted.permalink,
                        ),
                    )
                    .limit(1)
                )
                if post is None:
                    errors.append(
                        {
                            "record_key": raw_record.record_key,
                            "error": "normalized_post_missing",
                        }
                    )
                    continue
                linked_count = session.scalar(
                    select(func.count(PostMedia.media_asset_id)).where(PostMedia.post_id == post.id)
                )
                duplicate_events = session.scalars(
                    select(AuditEvent.details_json).where(
                        AuditEvent.event_type == "duplicate_media_reference_skipped",
                        AuditEvent.entity_type == "post",
                        AuditEvent.entity_id == post.id,
                    )
                ).all()
                duplicate_positions: set[int] = set()
                for details_json in duplicate_events:
                    try:
                        position = json.loads(details_json).get("position")
                    except (AttributeError, json.JSONDecodeError):
                        continue
                    if isinstance(position, int):
                        duplicate_positions.add(position)
                expected_linked_count = max(
                    0,
                    len(extracted.images) - len(duplicate_positions),
                )
                if int(linked_count or 0) != expected_linked_count:
                    errors.append(
                        {
                            "record_key": raw_record.record_key,
                            "error": "snapshot_media_link_mismatch",
                            "snapshot_images": len(extracted.images),
                            "linked_media": int(linked_count or 0),
                            "deduplicated_references": len(duplicate_positions),
                            "expected_linked_media": expected_linked_count,
                        }
                    )
                    continue
                if dry_run:
                    reparsed += 1
                    continue
                extracted.raw_dom_snapshot_path = relative
                raw_record.raw_json = extracted.model_dump_json()
                post.caption = extracted.caption
                post.displayed_date_text = extracted.displayed_date_text
                if post.published_at is None or extracted.date_precision == DatePrecision.EXACT:
                    post.published_at = extracted.published_at
                post.date_precision = extracted.date_precision.value
                post.like_count = extracted.like_count
                post.comment_count = extracted.comment_count
                post.post_type = extracted.post_type.value
                post.raw_engagement_json = json.dumps(
                    {
                        "likes": extracted.raw_like_text,
                        "comments": extracted.raw_comment_text,
                    },
                    sort_keys=True,
                )
                audit(
                    session,
                    "post_reparsed_from_snapshot",
                    "post",
                    post.id,
                    {"adapter_version": self.adapter.version, "capture_run_id": run.id},
                )
                reparsed += 1

            if not dry_run:
                cursor = self._cursor(run)
                cursor["snapshot_reparse"] = {
                    "adapter_version": self.adapter.version,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "errors": len(errors),
                    "reparsed": reparsed,
                    "scanned": scanned,
                }
                run.last_cursor_json = json.dumps(cursor, sort_keys=True)
                if not errors:
                    run.selector_adapter_version = self.adapter.version
                audit(
                    session,
                    "capture_snapshots_reparsed",
                    "capture_run",
                    run.id,
                    {
                        "adapter_version": self.adapter.version,
                        "errors": len(errors),
                        "reparsed": reparsed,
                        "scanned": scanned,
                    },
                )
            return {
                "run_id": run.id,
                "adapter_version": self.adapter.version,
                "dry_run": dry_run,
                "scanned": scanned,
                "reparsed": reparsed,
                "errors": errors,
            }

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
            statuses = [
                RunStatus.RUNNING.value,
                RunStatus.PAUSED.value,
                RunStatus.FAILED.value,
            ]
            # A live feed can look complete while YouTube is temporarily paused
            # at a continuation boundary. Explicit --resume must therefore be
            # able to reopen the latest managed/CDP checkpoint in place.
            if mode in {CaptureMode.MANAGED_BROWSER, CaptureMode.CDP}:
                statuses.append(RunStatus.COMPLETED.value)
            run = session.scalar(
                select(CaptureRun)
                .where(
                    CaptureRun.channel_id == channel.id,
                    CaptureRun.mode == mode.value,
                    CaptureRun.status.in_(statuses),
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
            run.completed_at = None
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
        for diagnostic in diagnostics:
            digest = hashlib.sha256(diagnostic.model_dump_json().encode("utf-8")).hexdigest()[:16]
            key = f"diagnostic-{diagnostic.code}-{digest}"
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
    ) -> bool:
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
                    raw_dom_snapshot_path=record.raw_dom_snapshot_path,
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
        resolved_reference_count = 0
        linked_asset_ids: set[int] = set()
        for position, reference in enumerate(record.images):
            try:
                asset, downloaded = self._ingest_reference(
                    session,
                    reference,
                    MediaKind.HISTORICAL,
                    fixture_assets,
                    source_page_url=record.permalink,
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
            resolved_reference_count += 1
            if asset.id in linked_asset_ids:
                audit(
                    session,
                    "duplicate_media_reference_skipped",
                    "post",
                    post.id,
                    {
                        "media_asset_id": asset.id,
                        "position": position,
                        "url": reference.url,
                    },
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
            linked_asset_ids.add(asset.id)
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
        return resolved_reference_count == len(record.images)

    def _ingest_reference(
        self,
        session: Session,
        reference: ImageReference,
        kind: MediaKind,
        fixture_assets: dict[str, Path] | None,
        *,
        source_page_url: str | None = None,
    ) -> tuple[MediaAsset, bool]:
        source = self._resolve_reference(reference, fixture_assets)
        features = inspect_image(source)
        existing = session.scalar(
            select(MediaAsset).where(MediaAsset.sha256 == features.sha256).limit(1)
        )
        if existing is not None:
            if source_page_url and not existing.source_page_url:
                existing.source_page_url = source_page_url
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
            source_page_url=source_page_url,
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
            if content_type not in {"image/jpeg", "image/png", "image/webp", "image/gif"}:
                raise ValueError(f"unsupported MIME type {content_type or 'unknown'}")
            if len(response.content) > self.settings.maximum_image_bytes:
                raise ValueError("image exceeds configured maximum size")
            digest = hashlib.sha256(response.content).hexdigest()
            extension = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
                "image/gif": ".gif",
            }[content_type]
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
