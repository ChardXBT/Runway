from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import func, select

from runway.captions.preference_models import PreferenceModelService
from runway.db.base import Database
from runway.db.models import PairwisePreference


@dataclass(frozen=True)
class PreferenceScore:
    score: float
    trained: bool
    calibrated: bool
    reason: str
    sample_count: int
    model_version_id: int | None = None


class CaptionPreferenceRanker(Protocol):
    def score(
        self,
        *,
        channel_id: int,
        text: str,
        components: dict[str, float],
    ) -> PreferenceScore: ...


class ImagePreferenceRanker(Protocol):
    def score_image(
        self,
        *,
        channel_id: int,
        features: dict[str, object],
    ) -> PreferenceScore: ...


class ImageCaptionPairRanker(Protocol):
    def score_pair(
        self,
        *,
        image_score: float,
        caption_score: float,
        grounding_score: float,
    ) -> PreferenceScore: ...


class PairwiseCaptionPreferenceRanker:
    """Load an explicitly activated persisted model or use the fixed fallback."""

    def __init__(self, database: Database):
        self.database = database
        self.models = PreferenceModelService(database)

    def score(
        self,
        *,
        channel_id: int,
        text: str,
        components: dict[str, float],
    ) -> PreferenceScore:
        persisted = self.models.score_caption(
            channel_id=channel_id,
            text=text,
            components=components,
        )
        if persisted is not None:
            return PreferenceScore(
                score=persisted.score,
                trained=True,
                calibrated=persisted.calibrated,
                reason=persisted.reason,
                sample_count=persisted.sample_count,
                model_version_id=persisted.model_version_id,
            )
        with self.database.session() as session:
            label_count = int(
                session.scalar(
                    select(func.count(PairwisePreference.id)).where(
                        PairwisePreference.channel_id == channel_id,
                        PairwisePreference.target == "caption",
                    )
                )
                or 0
            )
        return PreferenceScore(
            score=self._fallback(text, components),
            trained=False,
            calibrated=False,
            reason=(
                "fixed deterministic fallback; no active persisted caption "
                f"preference model ({label_count} labels)"
            ),
            sample_count=label_count,
        )

    @staticmethod
    def _fallback(text: str, components: dict[str, float]) -> float:
        del text
        structure_score = components.get("structure_fit", 0.5)
        value = (
            0.24 * structure_score
            + 0.25 * components.get("grounding", 0.5)
            + 0.15 * components.get("policy", 0.5)
            + 0.12 * components.get("style", 0.5)
            + 0.09 * components.get("novelty", 0.5)
            + 0.05 * components.get("rotation", 0.5)
            + 0.06 * components.get("positive_feedback", 0.0)
            + 0.04 * components.get("pairing", 0.5)
            - 0.08 * components.get("negative_feedback_risk", 0.0)
        )
        return max(0.0, min(1.0, value))


class DeterministicImageCaptionPairRanker:
    def score_pair(
        self,
        *,
        image_score: float,
        caption_score: float,
        grounding_score: float,
    ) -> PreferenceScore:
        value = 0.35 * image_score + 0.35 * caption_score + 0.30 * grounding_score
        return PreferenceScore(
            score=max(0.0, min(1.0, value)),
            trained=False,
            calibrated=False,
            reason="deterministic image-caption compatibility baseline",
            sample_count=0,
        )
