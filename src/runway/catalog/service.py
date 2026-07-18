from __future__ import annotations

import json
from collections import Counter, defaultdict
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, or_, select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    AuditEvent,
    CaptureRun,
    MediaAsset,
    Post,
    PostAnnotation,
    PostMedia,
    RawPostRecord,
)
from runway.media.service import hamming_similarity


class CatalogService:
    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    def status(self) -> dict[str, object]:
        with self.database.session() as session:
            total = session.scalar(select(func.count(Post.id))) or 0
            eligible = (
                session.scalar(
                    select(func.count(Post.id)).where(Post.is_training_eligible.is_(True))
                )
                or 0
            )
            assets = session.scalar(select(func.count(MediaAsset.id))) or 0
            last_capture = session.scalar(
                select(CaptureRun).order_by(desc(CaptureRun.started_at)).limit(1)
            )
            return {
                "total_posts": total,
                "training_eligible": eligible,
                "media_assets": assets,
                "last_capture": (
                    {
                        "id": last_capture.id,
                        "status": last_capture.status,
                        "posts_seen": last_capture.posts_seen,
                        "completed_at": (
                            last_capture.completed_at.isoformat()
                            if last_capture.completed_at
                            else None
                        ),
                    }
                    if last_capture
                    else None
                ),
            }

    def list_posts(
        self,
        *,
        limit: int = 20,
        offset: int = 0,
        search: str | None = None,
        post_type: str | None = None,
        training_eligible: bool | None = None,
        franchise: str | None = None,
        character: str | None = None,
    ) -> list[dict[str, object]]:
        with self.database.session() as session:
            statement = select(Post).order_by(desc(Post.published_at), desc(Post.id))
            if search:
                statement = statement.where(Post.caption.ilike(f"%{search}%"))
            if post_type:
                statement = statement.where(Post.post_type == post_type)
            if training_eligible is not None:
                statement = statement.where(Post.is_training_eligible.is_(training_eligible))
            if franchise or character:
                statement = statement.join(PostAnnotation, PostAnnotation.post_id == Post.id)
            if franchise:
                statement = statement.where(PostAnnotation.franchise.ilike(f"%{franchise}%"))
            if character:
                statement = statement.where(PostAnnotation.characters_json.ilike(f"%{character}%"))
            posts = session.scalars(statement.offset(offset).limit(limit)).all()
            return [self._post_summary(session, post) for post in posts]

    def detail(self, post_id: int) -> dict[str, object]:
        with self.database.session() as session:
            post = session.get(Post, post_id)
            if post is None:
                raise LookupError(f"post {post_id} not found")
            result = self._post_summary(session, post)
            result.update(
                {
                    "displayed_date_text": post.displayed_date_text,
                    "date_precision": post.date_precision,
                    "like_count": post.like_count,
                    "comment_count": post.comment_count,
                    "raw_engagement": json.loads(post.raw_engagement_json or "{}"),
                    "source_capture_run_id": post.source_capture_run_id,
                    "created_at": post.created_at.isoformat(),
                    "updated_at": post.updated_at.isoformat(),
                }
            )
            return result

    def set_training_eligibility(self, post_id: int, eligible: bool) -> dict[str, object]:
        with self.database.session() as session:
            post = session.get(Post, post_id)
            if post is None:
                raise LookupError(f"post {post_id} not found")
            post.is_training_eligible = eligible
            session.add(
                AuditEvent(
                    event_type="catalogue_eligibility_changed",
                    entity_type="post",
                    entity_id=post.id,
                    details_json=json.dumps({"is_training_eligible": eligible}),
                )
            )
        return self.detail(post_id)

    def verify(self, *, write_reports: bool = True) -> dict[str, Any]:
        with self.database.session() as session:
            posts = session.scalars(select(Post).order_by(Post.id)).all()
            media = session.scalars(
                select(MediaAsset).where(MediaAsset.kind == "historical").order_by(MediaAsset.id)
            ).all()
            links = session.execute(select(PostMedia.post_id, PostMedia.media_asset_id)).all()
            post_to_assets: dict[int, list[int]] = defaultdict(list)
            asset_to_posts: dict[int, list[int]] = defaultdict(list)
            for post_id, asset_id in links:
                post_to_assets[post_id].append(asset_id)
                asset_to_posts[asset_id].append(post_id)

            exact_duplicates = [
                {"media_asset_id": asset_id, "post_ids": sorted(post_ids)}
                for asset_id, post_ids in sorted(asset_to_posts.items())
                if len(set(post_ids)) > 1
            ]
            near_clusters = self._near_duplicate_clusters(media)
            broken = [
                {"id": asset.id, "local_path": asset.local_path}
                for asset in media
                if not (self.settings.resolved_data_dir / asset.local_path).is_file()
            ]
            external_counter = Counter(
                post.external_post_id for post in posts if post.external_post_id is not None
            )
            duplicate_external = [value for value, count in external_counter.items() if count > 1]
            raw_records = session.scalars(select(RawPostRecord)).all()
            post_external = {post.external_post_id for post in posts if post.external_post_id}
            post_links = {post.permalink for post in posts if post.permalink}
            unmatched_raw = [
                raw.id
                for raw in raw_records
                if not (
                    (raw.external_post_id and raw.external_post_id in post_external)
                    or (raw.permalink and raw.permalink in post_links)
                )
            ]
            capture_runs = session.scalars(
                select(CaptureRun).where(
                    or_(CaptureRun.status == "failed", CaptureRun.error_summary.is_not(None))
                )
            ).all()
            diagnostics = session.scalars(
                select(AuditEvent).where(AuditEvent.event_type == "capture_diagnostic")
            ).all()
            dates = sorted(post.published_at for post in posts if post.published_at is not None)
            precision = Counter(post.date_precision for post in posts)
            report: dict[str, Any] = {
                "generated_at": datetime.now().astimezone().isoformat(),
                "total_posts": len(posts),
                "training_eligible_image_posts": sum(post.is_training_eligible for post in posts),
                "date_range": {
                    "earliest": dates[0].isoformat() if dates else None,
                    "latest": dates[-1].isoformat() if dates else None,
                },
                "date_precision_distribution": dict(sorted(precision.items())),
                "posts_missing_captions": [post.id for post in posts if not post.caption],
                "posts_missing_images": [
                    post.id
                    for post in posts
                    if post.post_type in {"image", "multi_image"} and not post_to_assets[post.id]
                ],
                "broken_media_files": broken,
                "duplicate_external_ids": duplicate_external,
                "exact_duplicate_images": exact_duplicates,
                "near_duplicate_image_clusters": near_clusters,
                "unmatched_raw_records": unmatched_raw,
                "capture_errors": [
                    {"run_id": run.id, "status": run.status, "error": run.error_summary}
                    for run in capture_runs
                ],
                "capture_diagnostics": len(diagnostics),
            }
        if write_reports:
            self._write_report(report)
        return report

    def _post_summary(self, session: Any, post: Post) -> dict[str, object]:
        assets = session.scalars(
            select(MediaAsset)
            .join(PostMedia, PostMedia.media_asset_id == MediaAsset.id)
            .where(PostMedia.post_id == post.id)
            .order_by(PostMedia.position)
        ).all()
        return {
            "id": post.id,
            "external_post_id": post.external_post_id,
            "permalink": post.permalink,
            "post_type": post.post_type,
            "caption": post.caption,
            "published_at": post.published_at.isoformat() if post.published_at else None,
            "date_precision": post.date_precision,
            "is_training_eligible": post.is_training_eligible,
            "media": [
                {
                    "id": asset.id,
                    "url": f"/media/{Path(asset.local_path).relative_to('media').as_posix()}",
                    "local_path": asset.local_path,
                    "width": asset.width,
                    "height": asset.height,
                    "sha256": asset.sha256,
                    "perceptual_hash": asset.perceptual_hash,
                }
                for asset in assets
            ],
        }

    @staticmethod
    def _near_duplicate_clusters(media: Sequence[MediaAsset]) -> list[dict[str, object]]:
        adjacency: dict[int, set[int]] = defaultdict(set)
        similarities: dict[tuple[int, int], float] = {}
        for index, first in enumerate(media):
            for second in media[index + 1 :]:
                similarity = hamming_similarity(first.perceptual_hash, second.perceptual_hash)
                if similarity >= 0.9:
                    adjacency[first.id].add(second.id)
                    adjacency[second.id].add(first.id)
                    similarities[(min(first.id, second.id), max(first.id, second.id))] = similarity
        visited: set[int] = set()
        clusters: list[dict[str, object]] = []
        for asset_id in sorted(adjacency):
            if asset_id in visited:
                continue
            stack = [asset_id]
            members: set[int] = set()
            while stack:
                current = stack.pop()
                if current in visited:
                    continue
                visited.add(current)
                members.add(current)
                stack.extend(adjacency[current] - visited)
            pair_scores = [
                score
                for pair, score in similarities.items()
                if pair[0] in members and pair[1] in members
            ]
            clusters.append(
                {
                    "asset_ids": sorted(members),
                    "highest_similarity": round(max(pair_scores, default=0.0), 4),
                }
            )
        return clusters

    def _write_report(self, report: dict[str, Any]) -> None:
        reports = self.settings.resolved_data_dir / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        (reports / "catalog-verification.json").write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str), encoding="utf-8"
        )
        lines = [
            "# Catalogue verification",
            "",
            f"Generated: {report['generated_at']}",
            "",
            f"- Total posts: {report['total_posts']}",
            f"- Training-eligible image posts: {report['training_eligible_image_posts']}",
            f"- Date range: {report['date_range']['earliest']} to {report['date_range']['latest']}",
            "- Date precision: "
            f"`{json.dumps(report['date_precision_distribution'], sort_keys=True)}`",
            f"- Missing captions: {len(report['posts_missing_captions'])}",
            f"- Missing images: {len(report['posts_missing_images'])}",
            f"- Broken media files: {len(report['broken_media_files'])}",
            f"- Duplicate external IDs: {len(report['duplicate_external_ids'])}",
            f"- Exact duplicate image groups: {len(report['exact_duplicate_images'])}",
            f"- Near-duplicate clusters: {len(report['near_duplicate_image_clusters'])}",
            f"- Unmatched raw records: {len(report['unmatched_raw_records'])}",
            f"- Capture errors: {len(report['capture_errors'])}",
            f"- Non-fatal capture diagnostics: {report['capture_diagnostics']}",
            "",
            "Exact dates are reported only when present in source data. "
            "Relative dates remain relative.",
        ]
        (reports / "catalog-verification.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
