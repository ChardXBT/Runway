from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

import numpy as np
from pydantic import BaseModel, Field
from sqlalchemy import select

from runway.analysis.schemas import CandidateAnalysis
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    BlockedSource,
    CandidateImage,
    FeedbackSignal,
    MediaAsset,
    Post,
    PostMedia,
    Proposal,
    StyleProfile,
)
from runway.db.repositories import get_channel
from runway.intelligence.embeddings import ActiveRepresentationResolver, cosine
from runway.intelligence.topic_eligibility import (
    TopicEligibilityResult,
    TopicEligibilityService,
)
from runway.media.service import ImageFeatures
from runway.ranking.duplicates import DuplicateResult


class RankingResult(BaseModel):
    hard_rejection_reason: str | None = None
    quality_score: float = Field(ge=0, le=1)
    style_score: float = Field(ge=0, le=1)
    novelty_score: float = Field(ge=0, le=1)
    caption_potential_score: float = Field(ge=0, le=1)
    source_risk_score: float = Field(ge=0, le=1)
    rotation_score: float = Field(ge=0, le=1)
    creator_image_preference_score: float = Field(ge=0, le=1)
    text_overlay_penalty: float = Field(ge=0, le=1)
    topic_eligibility_class: str
    topic_eligibility: dict[str, object]
    representation_provenance: dict[str, object]
    final_rank_score: float = Field(ge=0, le=1)
    warnings: list[str]
    selection_reason: str


class CandidateRanker:
    weights = {
        "style": 0.22,
        "topic": 0.1,
        "novelty": 0.22,
        "caption_potential": 0.2,
        "quality": 0.15,
        "rotation": 0.06,
        "creator_image_preference": 0.05,
    }

    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self.representations = ActiveRepresentationResolver(database)
        self.topic_eligibility = TopicEligibilityService(
            database,
            settings,
            self.representations,
        )

    def rank(
        self,
        features: ImageFeatures,
        analysis: CandidateAnalysis,
        duplicate: DuplicateResult,
        *,
        source_domain: str,
        rights_status: str,
        image_path: Path | None = None,
        preflight_rejection: str | None = None,
    ) -> RankingResult:
        del rights_status
        eligibility = self.topic_eligibility.evaluate(analysis)
        hard_reason = preflight_rejection or self._hard_filter(
            features,
            analysis,
            duplicate,
            source_domain=source_domain,
            eligibility=eligibility,
        )
        quality = self._quality(features)
        style, style_provenance = self._style_match(features, image_path=image_path)
        novelty = max(
            0.0,
            min(1.0, (1.0 - max(duplicate.highest_semantic_similarity, 0.0)) / 0.12),
        )
        caption_potential = analysis.caption_potential
        rotation = self._rotation(analysis)
        creator_image_preference, preference_provenance = self._creator_image_preference(
            features,
            image_path=image_path,
        )
        # Rights remain provenance metadata. They are not a content-safety signal and
        # therefore never reduce discovery rank.
        source_risk = 0.0
        topic = max(0.0, min(1.0, eligibility.semantic_score))
        text_penalty = 0.25 if analysis.text_overlay else 0.0
        weighted = (
            self.weights["style"] * style
            + self.weights["topic"] * topic
            + self.weights["novelty"] * novelty
            + self.weights["caption_potential"] * caption_potential
            + self.weights["quality"] * quality
            + self.weights["rotation"] * rotation
            + self.weights["creator_image_preference"] * creator_image_preference
            - text_penalty
            - eligibility.rank_penalty
        )
        final = 0.0 if hard_reason else max(0.0, min(1.0, weighted))
        warnings = list(duplicate.warnings)
        if eligibility.classification in {"adjacent", "exploratory", "off-topic"}:
            warnings.append(f"topic_eligibility:{eligibility.classification}")
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
            creator_image_preference_score=round(creator_image_preference, 6),
            text_overlay_penalty=text_penalty,
            topic_eligibility_class=eligibility.classification,
            topic_eligibility=eligibility.model_dump(),
            representation_provenance={
                "topic": eligibility.representation,
                "style": style_provenance,
                "creator_image_preference": preference_provenance,
            },
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
        eligibility: TopicEligibilityResult,
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
        if analysis.unsafe_probability >= 0.5:
            return "nsfw_content"
        if self._is_non_frame_merchandise(analysis):
            return "non_frame_merchandise"
        if self._has_intrusive_production_credit(analysis):
            return "intrusive_production_credit"
        if self._is_production_credit_title_card(analysis):
            return "production_credit_title_card"
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
        if eligibility.hard_reject:
            return "blocked_topic" if eligibility.classification == "blocked" else "off_topic"
        return None

    @staticmethod
    def _is_non_frame_merchandise(analysis: CandidateAnalysis) -> bool:
        """Reject obvious product snapshots while continuing to allow artwork and fan art."""

        scene = " ".join(
            (
                analysis.scene_archetype,
                analysis.composition,
                analysis.setting,
            )
        ).casefold()
        objects = " ".join(analysis.objects).casefold()
        photographic_display = "photograph" in scene and any(
            marker in scene for marker in ("collection", "display", "product")
        )
        merchandise_subject = any(
            marker in objects
            for marker in (
                "action figure",
                "doll",
                "merchandise",
                "plush",
                "toy",
            )
        )
        return photographic_display and merchandise_subject

    @staticmethod
    def _has_intrusive_production_credit(analysis: CandidateAnalysis) -> bool:
        if not analysis.text_overlay or not analysis.ocr_text:
            return False
        text = " ".join(analysis.ocr_text).casefold()
        return any(
            marker in text
            for marker in (
                "co-executive producer",
                "created by",
                "directed by",
                "executive producer",
                "producer",
                "produced by",
                "written by",
            )
        )

    @staticmethod
    def _is_production_credit_title_card(analysis: CandidateAnalysis) -> bool:
        scene = analysis.scene_archetype.casefold()
        return (
            not analysis.characters
            and analysis.text_overlay
            and ("credit" in scene or "title card" in scene)
        )

    def _supported_topics(self) -> set[str]:
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            profile = session.scalar(
                select(StyleProfile)
                .where(
                    StyleProfile.channel_id == channel_id,
                    StyleProfile.is_active.is_(True),
                )
                .order_by(StyleProfile.version.desc())
                .limit(1)
            )
            if profile is None:
                return set()
            payload = json.loads(profile.profile_json)
        statistics = payload.get("caption_statistics", {})
        sample_size = int(statistics.get("sample_size", 0)) if isinstance(statistics, dict) else 0
        minimum_support = max(2, round(sample_size * 0.01))
        distribution = payload.get(
            "topic_distribution",
            payload.get("franchise_distribution", []),
        )
        if not isinstance(distribution, list):
            return set()
        supported: set[str] = set()
        for row in distribution:
            if not isinstance(row, (list, tuple)) or len(row) < 2:
                continue
            name = self._normalized_topic(str(row[0]))
            try:
                count = int(row[1])
            except (TypeError, ValueError):
                continue
            if name and name not in {"unknown", "none", "null"} and count >= minimum_support:
                supported.add(name)
        return supported

    @classmethod
    def _candidate_topics(cls, analysis: CandidateAnalysis) -> set[str]:
        topics = {
            cls._normalized_topic(str(entity.canonical_name or entity.name))
            for entity in analysis.entities
            if entity.canonical_name or entity.name
        }
        if analysis.franchise:
            topics.add(cls._normalized_topic(analysis.franchise))
        return {value for value in topics if value}

    @staticmethod
    def _normalized_topic(value: str) -> str:
        return " ".join(value.strip().casefold().split())

    @staticmethod
    def _quality(features: ImageFeatures) -> float:
        resolution = min(1.0, min(features.width, features.height) / 1080)
        blur = min(1.0, features.blur_score / 700)
        compression = min(1.0, features.quality_metrics.get("bytes_per_pixel", 0) / 0.35)
        return max(0.0, min(1.0, 0.5 * resolution + 0.3 * blur + 0.2 * compression))

    def _style_match(
        self,
        features: ImageFeatures,
        *,
        image_path: Path | None,
    ) -> tuple[float, dict[str, object]]:
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
        _provider, resolution = self.representations.resolve(channel_id, modality="image")
        active_set = self.representations.store.active_set(
            channel_id=channel_id,
            scope="historical_image",
            purpose="historical_visual_semantics",
        )
        if active_set is not None:
            records = self.representations.store.records_for_set(active_set.id)
            if image_path is not None:
                query, _query_resolution = self.representations.image_vector(
                    channel_id,
                    image_path,
                    score_purpose="candidate_style_similarity",
                )
                query_resolution = "active_provider_query"
            elif resolution.provider == "runway-local":
                # The canonical local image set is built from this exact descriptor.
                query = np.asarray(features.embedding, dtype=np.float32)
                query_resolution = "compatible_local_descriptor"
            else:
                raise ValueError("neural image ranking requires the candidate image path")
            similarities = sorted(
                cosine(query, self.representations.store.vectors(record)[0]) for record in records
            )
            provenance = resolution.as_dict()
            provenance.update(
                {
                    "query_resolution": query_resolution,
                    "historical_record_count": len(records),
                }
            )
        else:
            # A database without an active set still uses the canonical resolver.
            # Legacy MediaAsset vectors remain available only to duplicate safeguards
            # and diagnostics; they never control semantic style ranking.
            with self.database.session() as session:
                historical = session.scalars(
                    select(MediaAsset)
                    .join(PostMedia, PostMedia.media_asset_id == MediaAsset.id)
                    .join(Post, Post.id == PostMedia.post_id)
                    .where(
                        Post.channel_id == channel_id,
                        MediaAsset.kind == "historical",
                    )
                ).all()
            if image_path is not None:
                query, _query_resolution = self.representations.image_vector(
                    channel_id,
                    image_path,
                    score_purpose="candidate_style_similarity",
                )
                query_resolution = "deterministic_provider_query"
            elif resolution.provider == "runway-local":
                query = np.asarray(features.embedding, dtype=np.float32)
                query_resolution = "compatible_local_descriptor"
            else:
                raise ValueError("neural image ranking requires the candidate image path")
            similarities = []
            skipped = 0
            for asset in historical:
                try:
                    historical_vector, _historical_resolution = self.representations.image_vector(
                        channel_id,
                        self._media_path(asset),
                        score_purpose="candidate_style_history",
                    )
                except (FileNotFoundError, ValueError):
                    skipped += 1
                    continue
                similarities.append(cosine(query, historical_vector))
            similarities.sort()
            provenance = resolution.as_dict()
            provenance.update(
                {
                    "query_resolution": query_resolution,
                    "historical_record_count": len(similarities),
                    "historical_files_skipped": skipped,
                    "legacy_media_vectors_used": False,
                }
            )
        top = similarities[-min(5, len(similarities)) :]
        if not top:
            return 0.5, provenance
        return max(0.0, min(1.0, sum(top) / len(top))), provenance

    def _rotation(self, analysis: CandidateAnalysis) -> float:
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            active_topics = session.scalars(
                select(CandidateImage.detected_topic_json)
                .join(Proposal, Proposal.candidate_image_id == CandidateImage.id)
                .where(
                    Proposal.channel_id == channel_id,
                    Proposal.status.not_in(
                        ["rejected", "cancelled", "published", "publish_failed"]
                    ),
                )
            ).all()
            counts = Counter(
                str(json.loads(topic).get("franchise") or "unknown") for topic in active_topics
            )
        if not counts or not analysis.franchise:
            return 1.0
        total = sum(counts.values())
        return 1.0 - counts.get(analysis.franchise, 0) / max(total + 1, 1)

    def _creator_image_preference(
        self,
        features: ImageFeatures,
        *,
        image_path: Path | None,
    ) -> tuple[float, dict[str, object]]:
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            signals = session.scalars(
                select(FeedbackSignal)
                .where(
                    FeedbackSignal.channel_id == channel_id,
                    FeedbackSignal.target == "image",
                    FeedbackSignal.candidate_image_id.is_not(None),
                )
                .order_by(FeedbackSignal.created_at.desc(), FeedbackSignal.id.desc())
                .limit(100)
            ).all()
            candidate_ids = {
                signal.candidate_image_id
                for signal in signals
                if signal.candidate_image_id is not None
            }
            candidates = {
                candidate.id: candidate
                for candidate in session.scalars(
                    select(CandidateImage).where(CandidateImage.id.in_(candidate_ids))
                )
            }
            media_ids = {candidate.media_asset_id for candidate in candidates.values()}
            media = {
                asset.id: asset
                for asset in session.scalars(select(MediaAsset).where(MediaAsset.id.in_(media_ids)))
            }
        _provider, resolution = self.representations.resolve(channel_id, modality="image")
        if image_path is not None:
            query, _query_resolution = self.representations.image_vector(
                channel_id,
                image_path,
                score_purpose="creator_image_preference_query",
            )
        elif resolution.provider == "runway-local":
            query = np.asarray(features.embedding, dtype=np.float32)
        else:
            raise ValueError("neural image-preference ranking requires the candidate image path")
        positive = 0.0
        negative = 0.0
        scored = 0
        for signal in signals:
            candidate = candidates.get(signal.candidate_image_id or -1)
            asset = media.get(candidate.media_asset_id) if candidate else None
            if asset is None:
                continue
            try:
                other, _other_resolution = self.representations.image_vector(
                    channel_id,
                    self._media_path(asset),
                    score_purpose="creator_image_preference_example",
                )
            except (FileNotFoundError, ValueError):
                continue
            similarity = max(0.0, cosine(query, other))
            scored += 1
            if signal.verdict == "accepted":
                positive = max(positive, similarity)
            elif signal.verdict == "rejected":
                negative = max(negative, similarity)
        if positive == 0.0 and negative == 0.0:
            score = 0.5
        else:
            score = max(0.0, min(1.0, 0.5 + 0.5 * (positive - negative)))
        provenance = resolution.as_dict()
        provenance["feedback_examples_scored"] = scored
        return score, provenance

    def _media_path(self, media: MediaAsset) -> Path:
        raw = Path(media.local_path)
        resolved = (raw if raw.is_absolute() else self.settings.resolved_data_dir / raw).resolve()
        root = self.settings.resolved_data_dir.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError(f"media asset {media.id} escapes the configured data root")
        if not resolved.is_file():
            raise FileNotFoundError(resolved)
        return resolved


def ranking_weights_json() -> str:
    return json.dumps(CandidateRanker.weights, sort_keys=True)
