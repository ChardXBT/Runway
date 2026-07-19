from __future__ import annotations

import asyncio
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runway.analysis.service import AnalysisService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    AnnotationRefreshItem,
    AnnotationRefreshRun,
    IntelligenceActivation,
    MediaAsset,
    Post,
    PostAnnotation,
    PostMedia,
    utcnow,
)
from runway.db.repositories import get_channel
from runway.intelligence.embeddings import configuration_hash


class AnnotationRefreshService:
    def __init__(self, database: Database, settings: Settings | None = None):
        self.database = database
        self.settings = settings or database.settings

    def plan(
        self,
        *,
        annotation_version: str = AnalysisService.annotation_version,
        prompt_version: str = AnalysisService.prompt_version,
    ) -> dict[str, object]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            posts = session.scalars(
                select(Post)
                .where(
                    Post.channel_id == channel.id,
                    Post.is_training_eligible.is_(True),
                )
                .order_by(Post.id)
            ).all()
            source_hashes = {post.id: self._post_source_hash(session, post) for post in posts}
            existing_annotations = {
                row.post_id: row.id
                for row in session.scalars(
                    select(PostAnnotation).where(
                        PostAnnotation.post_id.in_([post.id for post in posts]),
                        PostAnnotation.annotation_version == annotation_version,
                    )
                ).all()
            }
            plan_hash = configuration_hash(
                {
                    "annotation_version": annotation_version,
                    "prompt_version": prompt_version,
                    "provider": self.settings.agent_runtime,
                    "model": (
                        self.settings.codex_model
                        if self.settings.agent_runtime == "codex"
                        else self.settings.agent_runtime
                    ),
                    "items": [
                        {
                            "post_id": post.id,
                            "source_content_hash": source_hashes[post.id],
                        }
                        for post in posts
                    ],
                }
            )
            existing = session.scalar(
                select(AnnotationRefreshRun)
                .where(
                    AnnotationRefreshRun.channel_id == channel.id,
                    AnnotationRefreshRun.annotation_version == annotation_version,
                    AnnotationRefreshRun.plan_hash == plan_hash,
                )
                .limit(1)
            )
            if existing is not None:
                run_id = existing.id
                created = False
            else:
                run = AnnotationRefreshRun(
                    channel_id=channel.id,
                    annotation_version=annotation_version,
                    prompt_version=prompt_version,
                    provider=self.settings.agent_runtime,
                    model=(
                        self.settings.codex_model
                        if self.settings.agent_runtime == "codex"
                        else self.settings.agent_runtime
                    ),
                    configuration_json=json.dumps(
                        {
                            "analysis_batch_size": self.settings.analysis_batch_size,
                            "runtime": self.settings.agent_runtime,
                            "paid_api_fallback_enabled": False,
                        },
                        sort_keys=True,
                    ),
                    plan_hash=plan_hash,
                    status="planned",
                    expected_count=len(posts),
                    completed_count=len(existing_annotations),
                    failed_count=0,
                    active=False,
                )
                session.add(run)
                session.flush()
                session.add_all(
                    [
                        AnnotationRefreshItem(
                            annotation_refresh_run_id=run.id,
                            post_id=post.id,
                            source_content_hash=source_hashes[post.id],
                            status=("complete" if post.id in existing_annotations else "pending"),
                            post_annotation_id=existing_annotations.get(post.id),
                            attempts=0,
                            completed_at=(utcnow() if post.id in existing_annotations else None),
                        )
                        for post in posts
                    ]
                )
                run_id = run.id
                created = True
        return self.status(run_id) | {"created": created}

    async def run_batch(
        self,
        refresh_run_id: int,
        *,
        batch_size: int = 10,
        allow_model_calls: bool = False,
    ) -> dict[str, object]:
        if batch_size < 1 or batch_size > 100:
            raise ValueError("batch_size must be between 1 and 100")
        if self.settings.agent_runtime != "mock" and not allow_model_calls:
            raise PermissionError("real annotation refresh requires explicit --allow-model-calls")
        with self.database.session() as session:
            run = session.get(AnnotationRefreshRun, refresh_run_id)
            if run is None:
                raise LookupError(f"annotation refresh run {refresh_run_id} was not found")
            if run.active:
                raise ValueError("an active annotation refresh is immutable")
            pending_items = session.scalars(
                select(AnnotationRefreshItem)
                .where(
                    AnnotationRefreshItem.annotation_refresh_run_id == refresh_run_id,
                    AnnotationRefreshItem.status.in_(("pending", "failed")),
                )
                .order_by(AnnotationRefreshItem.id)
                .limit(batch_size)
            ).all()
            for item in pending_items:
                item.status = "running"
                item.attempts += 1
                item.error_summary = None
            pending = len(pending_items)
            run.status = "running"
        if pending:
            analysis = AnalysisService(self.database, self.settings)
            await analysis.analyze_history(
                resume=True,
                max_posts=pending,
            )
        self._refresh_items(refresh_run_id)
        return self.status(refresh_run_id)

    def run_batch_sync(
        self,
        refresh_run_id: int,
        *,
        batch_size: int = 10,
        allow_model_calls: bool = False,
    ) -> dict[str, object]:
        return asyncio.run(
            self.run_batch(
                refresh_run_id,
                batch_size=batch_size,
                allow_model_calls=allow_model_calls,
            )
        )

    def validate(self, refresh_run_id: int) -> dict[str, object]:
        self._refresh_items(refresh_run_id)
        errors: list[str] = []
        with self.database.session() as session:
            run = session.get(AnnotationRefreshRun, refresh_run_id)
            if run is None:
                raise LookupError(f"annotation refresh run {refresh_run_id} was not found")
            items = session.scalars(
                select(AnnotationRefreshItem)
                .where(AnnotationRefreshItem.annotation_refresh_run_id == refresh_run_id)
                .order_by(AnnotationRefreshItem.id)
            ).all()
            if len(items) != run.expected_count:
                errors.append("annotation plan item count does not match")
            for item in items:
                if item.status != "complete" or item.post_annotation_id is None:
                    errors.append(f"item {item.id} is {item.status}")
                    continue
                annotation = session.get(
                    PostAnnotation,
                    item.post_annotation_id,
                )
                if (
                    annotation is None
                    or annotation.post_id != item.post_id
                    or annotation.annotation_version != run.annotation_version
                ):
                    errors.append(f"item {item.id} has mismatched annotation lineage")
            valid = not errors
            if valid:
                run.status = "ready"
                run.validated_at = utcnow()
                run.error_summary = None
            else:
                run.status = (
                    "running" if any(item.status == "pending" for item in items) else "failed"
                )
                run.error_summary = "; ".join(errors[:20])
        return self.status(refresh_run_id) | {
            "valid": valid,
            "errors": errors,
        }

    def activate(
        self,
        refresh_run_id: int,
        *,
        reason: str,
    ) -> dict[str, object]:
        if not reason.strip():
            raise ValueError("activation reason is required")
        with self.database.session() as session:
            target = session.get(AnnotationRefreshRun, refresh_run_id)
            if (
                target is None
                or target.status != "ready"
                or target.completed_count != target.expected_count
                or target.failed_count
            ):
                raise ValueError("only a complete validated annotation refresh can activate")
            active_rows = session.scalars(
                select(AnnotationRefreshRun)
                .where(
                    AnnotationRefreshRun.channel_id == target.channel_id,
                    AnnotationRefreshRun.active.is_(True),
                    AnnotationRefreshRun.id != target.id,
                )
                .order_by(
                    AnnotationRefreshRun.activated_at.desc(),
                    AnnotationRefreshRun.id.desc(),
                )
            ).all()
            if len(active_rows) > 1:
                raise RuntimeError("multiple active annotation refresh runs exist")
            previous = active_rows[0] if active_rows else None
            if previous is not None:
                previous.active = False
                previous.status = "superseded"
            target.active = True
            target.status = "active"
            target.activated_at = utcnow()
            session.add(
                IntelligenceActivation(
                    channel_id=target.channel_id,
                    resource_type="annotation_refresh",
                    target="historical_annotation",
                    action="activate",
                    resource_id=str(target.id),
                    previous_resource_id=(str(previous.id) if previous is not None else None),
                    reason=reason.strip(),
                    gate_results_json=json.dumps(
                        {
                            "complete": target.completed_count,
                            "expected": target.expected_count,
                        },
                        sort_keys=True,
                    ),
                )
            )
        return self.status(refresh_run_id)

    def status(self, refresh_run_id: int) -> dict[str, object]:
        with self.database.session() as session:
            run = session.get(AnnotationRefreshRun, refresh_run_id)
            if run is None:
                raise LookupError(f"annotation refresh run {refresh_run_id} was not found")
            count_rows = session.execute(
                select(
                    AnnotationRefreshItem.status,
                    func.count(AnnotationRefreshItem.id),
                )
                .where(
                    AnnotationRefreshItem.annotation_refresh_run_id
                    == refresh_run_id
                )
                .group_by(AnnotationRefreshItem.status)
            ).all()
            counts: dict[str, int] = {
                str(status): int(count) for status, count in count_rows
            }
            return {
                "annotation_refresh_run_id": run.id,
                "annotation_version": run.annotation_version,
                "prompt_version": run.prompt_version,
                "provider": run.provider,
                "model": run.model,
                "plan_hash": run.plan_hash,
                "status": run.status,
                "active": run.active,
                "expected": run.expected_count,
                "complete": int(counts.get("complete", 0)),
                "failed": int(counts.get("failed", 0)),
                "stale": int(counts.get("stale", 0)),
                "pending": int(counts.get("pending", 0)),
                "coverage": (
                    int(counts.get("complete", 0)) / run.expected_count
                    if run.expected_count
                    else 1.0
                ),
                "error_summary": run.error_summary,
            }

    def _refresh_items(self, refresh_run_id: int) -> None:
        with self.database.session() as session:
            run = session.get(AnnotationRefreshRun, refresh_run_id)
            if run is None:
                raise LookupError(f"annotation refresh run {refresh_run_id} was not found")
            items = session.scalars(
                select(AnnotationRefreshItem)
                .where(AnnotationRefreshItem.annotation_refresh_run_id == refresh_run_id)
                .order_by(AnnotationRefreshItem.id)
            ).all()
            annotations = {
                row.post_id: row
                for row in session.scalars(
                    select(PostAnnotation).where(
                        PostAnnotation.post_id.in_([item.post_id for item in items]),
                        PostAnnotation.annotation_version == run.annotation_version,
                    )
                ).all()
            }
            for item in items:
                post = session.get(Post, item.post_id)
                if post is None:
                    item.status = "failed"
                    item.error_summary = "planned post was deleted"
                    continue
                current_hash = self._post_source_hash(session, post)
                if current_hash != item.source_content_hash:
                    item.status = "stale"
                    item.error_summary = "post content changed after planning"
                    continue
                annotation = annotations.get(item.post_id)
                if annotation is not None:
                    item.status = "complete"
                    item.post_annotation_id = annotation.id
                    item.completed_at = item.completed_at or utcnow()
                    item.error_summary = None
                elif item.status == "running":
                    item.status = "failed"
                    item.error_summary = "model call completed without an annotation"
            run.completed_count = sum(item.status == "complete" for item in items)
            run.failed_count = sum(item.status == "failed" for item in items)
            if run.completed_count == run.expected_count and not run.failed_count:
                run.status = "backfilled"
            elif any(item.status == "pending" for item in items):
                run.status = "running"
            else:
                run.status = "failed"

    @staticmethod
    def _post_source_hash(session: Session, post: Post) -> str:
        media_hashes = list(
            session.scalars(
                select(MediaAsset.sha256)
                .join(PostMedia, PostMedia.media_asset_id == MediaAsset.id)
                .where(PostMedia.post_id == post.id)
                .order_by(PostMedia.position, MediaAsset.id)
            ).all()
        )
        return configuration_hash(
            {
                "post_id": post.id,
                "caption": post.caption or "",
                "media_sha256": media_hashes,
            }
        )
