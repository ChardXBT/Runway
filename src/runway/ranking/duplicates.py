from __future__ import annotations

from datetime import UTC, datetime, timedelta

from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import CandidateImage, MediaAsset, Post, PostMedia, SearchRun
from runway.db.repositories import get_channel
from runway.media.service import (
    ImageFeatures,
    cosine_similarity,
    crop_descriptor_similarity,
    hamming_similarity,
)


class DuplicateResult(BaseModel):
    is_exact: bool
    is_transformed_duplicate: bool
    highest_visual_similarity: float = Field(ge=0, le=1)
    highest_semantic_similarity: float = Field(ge=-1, le=1)
    closest_asset_ids: list[int]
    within_180_day_window: bool | None
    hard_block: bool
    warnings: list[str]


class DuplicateDetector:
    transformed_semantic_threshold = 0.997
    crop_threshold = 0.9985

    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    def inspect(
        self,
        features: ImageFeatures,
        *,
        source_url: str | None = None,
        exclude_asset_id: int | None = None,
        reference_time: datetime | None = None,
    ) -> DuplicateResult:
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            historical_asset_ids = (
                select(PostMedia.media_asset_id)
                .join(Post, Post.id == PostMedia.post_id)
                .where(Post.channel_id == channel_id)
            )
            candidate_asset_ids = (
                select(CandidateImage.media_asset_id)
                .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
                .where(SearchRun.channel_id == channel_id)
            )
            statement = select(MediaAsset).where(
                or_(
                    MediaAsset.id.in_(historical_asset_ids),
                    MediaAsset.id.in_(candidate_asset_ids),
                )
            )
            if exclude_asset_id is not None:
                statement = statement.where(MediaAsset.id != exclude_asset_id)
            assets = session.scalars(statement).all()
            same_source = False
            if source_url:
                same_source = bool(
                    session.scalar(
                        select(MediaAsset.id)
                        .where(
                            MediaAsset.original_url == source_url,
                            or_(
                                MediaAsset.id.in_(historical_asset_ids),
                                MediaAsset.id.in_(candidate_asset_ids),
                            ),
                        )
                        .limit(1)
                    )
                    or session.scalar(
                        select(CandidateImage.id)
                        .join(SearchRun, SearchRun.id == CandidateImage.search_run_id)
                        .where(
                            SearchRun.channel_id == channel_id,
                            CandidateImage.direct_image_url == source_url,
                        )
                        .limit(1)
                    )
                )

            is_exact = False
            transformed = False
            highest_visual = 0.0
            highest_semantic = -1.0
            scored: list[tuple[float, int, float, float, float]] = []
            for asset in assets:
                if asset.sha256 == features.sha256:
                    is_exact = True
                phash = hamming_similarity(asset.perceptual_hash, features.perceptual_hash)
                crop = crop_descriptor_similarity(
                    asset.crop_resistant_hash, features.crop_resistant_hash
                )
                semantic = (
                    cosine_similarity(asset.embedding_vector, features.embedding)
                    if asset.embedding_vector
                    else 0.0
                )
                visual = max(phash, crop, semantic)
                scored.append((visual, asset.id, phash, crop, semantic))
                highest_visual = max(highest_visual, visual)
                highest_semantic = max(highest_semantic, semantic)
                transformed = transformed or (
                    phash >= self.settings.duplicate_perceptual_threshold
                    or crop >= self.crop_threshold
                    or semantic >= self.transformed_semantic_threshold
                )

            scored.sort(reverse=True)
            closest = [asset_id for _score, asset_id, _p, _c, _s in scored[:5]]
            within_window, date_warning = self._within_window(
                session,
                closest,
                reference_time or datetime.now(UTC),
                channel_id,
            )
            semantic_recent = (
                highest_semantic >= self.settings.duplicate_semantic_threshold
                and within_window is True
            )
            warnings: list[str] = []
            if date_warning:
                warnings.append(date_warning)
            if same_source:
                warnings.append("source URL was already used")
            if (
                highest_semantic >= self.settings.duplicate_semantic_threshold
                and within_window is False
            ):
                warnings.append("strong conceptual similarity is outside the hard 180-day window")
            hard_block = is_exact or transformed or same_source or semantic_recent
            return DuplicateResult(
                is_exact=is_exact,
                is_transformed_duplicate=transformed,
                highest_visual_similarity=round(highest_visual, 6),
                highest_semantic_similarity=round(highest_semantic, 6),
                closest_asset_ids=closest,
                within_180_day_window=within_window,
                hard_block=hard_block,
                warnings=warnings,
            )

    def _within_window(
        self,
        session: Session,
        asset_ids: list[int],
        reference_time: datetime,
        channel_id: int,
    ) -> tuple[bool | None, str | None]:
        if not asset_ids:
            return False, None
        rows = session.execute(
            select(Post.published_at, Post.date_precision)
            .join(PostMedia, PostMedia.post_id == Post.id)
            .where(
                Post.channel_id == channel_id,
                PostMedia.media_asset_id.in_(asset_ids),
            )
        ).all()
        if not rows:
            return False, None
        uncertain = False
        cutoff = reference_time - timedelta(days=self.settings.duplicate_window_days)
        for published_at, precision in rows:
            if published_at is None or precision in {"relative", "unknown"}:
                uncertain = True
                continue
            if published_at.tzinfo is None:
                published_at = published_at.replace(tzinfo=UTC)
            if published_at >= cutoff:
                return True, None
        if uncertain:
            return None, "historical date precision cannot prove the 180-day boundary"
        return False, None
