from __future__ import annotations

import json
from collections import Counter

from pydantic import BaseModel, Field
from sqlalchemy import select

from leeway.analysis.schemas import CandidateAnalysis
from leeway.config import Settings
from leeway.db.base import Database
from leeway.db.models import BlockedSource, MediaAsset, PostAnnotation
from leeway.media.service import ImageFeatures, cosine_similarity
from leeway.ranking.duplicates import DuplicateResult


class RankingResult(BaseModel):
    hard_rejection_reason: str | None = None
    quality_score: float = Field(ge=0, le=1)
    style_score: float = Field(ge=0, le=1)
    novelty_score: float = Field(ge=0, le=1)
    caption_potential_score: float = Field(ge=0, le=1)
    source_risk_score: float = Field(ge=0, le=1)
    rotation_score: float = Field(ge=0, le=1)
    text_overlay_penalty: float = Field(ge=0, le=1)
    final_rank_score: float = Field(ge=0, le=1)
    warnings: list[str]
    selection_reason: str


class CandidateRanker:
    weights = {
        "style": 0.25,
        "topic": 0.1,
        "novelty": 0.2,
        "caption_potential": 0.2,
        "quality": 0.15,
        "rotation": 0.05,
        "source_safety": 0.05,
    }

    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    def rank(
        self,
        features: ImageFeatures,
        analysis: CandidateAnalysis,
        duplicate: DuplicateResult,
        *,
        source_domain: str,
        rights_status: str,
    ) -> RankingResult:
        hard_reason = self._hard_filter(features, analysis, duplicate, source_domain=source_domain)
        quality = self._quality(features)
        style = self._style_match(features)
        novelty = max(
            0.0,
            min(1.0, (1.0 - max(duplicate.highest_semantic_similarity, 0.0)) / 0.12),
        )
        caption_potential = analysis.caption_potential
        rotation = self._rotation(analysis)
        source_risk = {"creator_owned": 0.05, "licensed": 0.1, "public_domain": 0.1}.get(
            rights_status, 0.45
        )
        topic = 0.8 if analysis.franchise else 0.55
        text_penalty = 0.25 if analysis.text_overlay else 0.0
        weighted = (
            self.weights["style"] * style
            + self.weights["topic"] * topic
            + self.weights["novelty"] * novelty
            + self.weights["caption_potential"] * caption_potential
            + self.weights["quality"] * quality
            + self.weights["rotation"] * rotation
            + self.weights["source_safety"] * (1 - source_risk)
            - text_penalty
        )
        final = 0.0 if hard_reason else max(0.0, min(1.0, weighted))
        warnings = list(duplicate.warnings)
        if rights_status == "unknown":
            warnings.append("rights status is unknown and requires human review")
        if analysis.watermark_probability > 0.25:
            warnings.append("possible watermark")
        if analysis.personal_artwork_probability > 0.25:
            warnings.append("possible independently created artwork")
        if analysis.fan_art_probability > 0.25:
            warnings.append("possible fan art")
        reason = (
            f"Selected from {source_domain}: style {style:.2f}, novelty {novelty:.2f}, "
            f"quality {quality:.2f}, caption potential {caption_potential:.2f}."
            if not hard_reason
            else f"Rejected by hard filter: {hard_reason}."
        )
        return RankingResult(
            hard_rejection_reason=hard_reason,
            quality_score=round(quality, 6),
            style_score=round(style, 6),
            novelty_score=round(novelty, 6),
            caption_potential_score=round(caption_potential, 6),
            source_risk_score=round(source_risk, 6),
            rotation_score=round(rotation, 6),
            text_overlay_penalty=text_penalty,
            final_rank_score=round(final, 6),
            warnings=warnings,
            selection_reason=reason,
        )

    def _hard_filter(
        self,
        features: ImageFeatures,
        analysis: CandidateAnalysis,
        duplicate: DuplicateResult,
        *,
        source_domain: str,
    ) -> str | None:
        if features.mime_type not in {"image/jpeg", "image/png", "image/webp"}:
            return "unsupported_mime_type"
        if features.file_size > self.settings.maximum_image_bytes:
            return "file_too_large"
        if min(features.width, features.height) < self.settings.minimum_image_dimension:
            return "resolution_below_threshold"
        if features.blur_score < 10:
            return "severe_blur"
        if max(features.width / features.height, features.height / features.width) > 2.2:
            return "unusable_aspect_ratio"
        if duplicate.hard_block:
            return "duplicate"
        with self.database.session() as session:
            blocked = session.scalar(
                select(BlockedSource.id).where(
                    BlockedSource.source_type == "domain", BlockedSource.value == source_domain
                )
            )
        if blocked:
            return "blocked_domain"
        if analysis.unsafe_probability >= 0.5:
            return "unsafe_content"
        if analysis.watermark_probability >= 0.65:
            return "prominent_watermark"
        if analysis.personal_artwork_probability >= 0.5:
            return "personal_artwork"
        if analysis.fan_art_probability >= 0.5:
            return "fan_art"
        return None

    @staticmethod
    def _quality(features: ImageFeatures) -> float:
        resolution = min(1.0, min(features.width, features.height) / 1080)
        blur = min(1.0, features.blur_score / 700)
        compression = min(1.0, features.quality_metrics.get("bytes_per_pixel", 0) / 0.35)
        return max(0.0, min(1.0, 0.5 * resolution + 0.3 * blur + 0.2 * compression))

    def _style_match(self, features: ImageFeatures) -> float:
        with self.database.session() as session:
            historical = session.scalars(
                select(MediaAsset).where(
                    MediaAsset.kind == "historical", MediaAsset.embedding_vector.is_not(None)
                )
            ).all()
        if not historical:
            return 0.5
        similarities = sorted(
            cosine_similarity(asset.embedding_vector or b"", features.embedding)
            for asset in historical
        )
        top = similarities[-min(5, len(similarities)) :]
        return max(0.0, min(1.0, sum(top) / len(top)))

    def _rotation(self, analysis: CandidateAnalysis) -> float:
        with self.database.session() as session:
            counts = Counter(
                annotation.franchise or "unknown"
                for annotation in session.scalars(select(PostAnnotation)).all()
            )
        if not counts or not analysis.franchise:
            return 0.6
        highest = max(counts.values())
        return 1.0 - counts.get(analysis.franchise, 0) / max(highest + 1, 1)


def ranking_weights_json() -> str:
    return json.dumps(CandidateRanker.weights, sort_keys=True)
