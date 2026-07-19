from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from sqlalchemy import select

from runway.captions.taxonomy import analyze_caption
from runway.db.base import Database
from runway.db.models import CaptionCandidateRecord, PairwisePreference

FEATURE_NAMES = (
    "bias",
    "open_question",
    "yes_no_question",
    "observation",
    "reaction",
    "explanation",
    "promotional",
    "length_log",
    "grounding",
    "policy",
    "style",
    "novelty",
    "rotation",
    "positive_feedback",
    "negative_feedback_risk",
    "pairing",
)


@dataclass(frozen=True)
class PreferenceScore:
    score: float
    trained: bool
    calibrated: bool
    reason: str
    sample_count: int


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
    """Small deterministic Bradley-Terry-style linear baseline."""

    minimum_pairs = 8

    def __init__(self, database: Database):
        self.database = database

    def score(
        self,
        *,
        channel_id: int,
        text: str,
        components: dict[str, float],
    ) -> PreferenceScore:
        rows = self._training_rows(channel_id)
        if len(rows) < self.minimum_pairs:
            fallback = self._fallback(text, components)
            return PreferenceScore(
                score=fallback,
                trained=False,
                calibrated=False,
                reason=f"deterministic fallback: {len(rows)} pairwise labels",
                sample_count=len(rows),
            )
        weights = self._fit(rows)
        vector = self._features(text, components)
        value = self._sigmoid(float(np.dot(weights, vector)))
        return PreferenceScore(
            score=value,
            trained=True,
            calibrated=False,
            reason="pairwise logistic baseline; probability is uncalibrated",
            sample_count=len(rows),
        )

    def _training_rows(
        self,
        channel_id: int,
    ) -> list[tuple[np.ndarray, np.ndarray]]:
        with self.database.session() as session:
            preferences = session.scalars(
                select(PairwisePreference)
                .where(PairwisePreference.channel_id == channel_id)
                .order_by(PairwisePreference.created_at, PairwisePreference.id)
                .limit(1000)
            ).all()
            candidate_ids = {
                value
                for row in preferences
                for value in (row.preferred_candidate_id, row.dispreferred_candidate_id)
                if value is not None
            }
            candidates = {
                row.id: row
                for row in session.scalars(
                    select(CaptionCandidateRecord).where(
                        CaptionCandidateRecord.id.in_(candidate_ids)
                    )
                )
            }
        result: list[tuple[np.ndarray, np.ndarray]] = []
        for row in preferences:
            preferred_record = (
                candidates.get(row.preferred_candidate_id)
                if row.preferred_candidate_id
                else None
            )
            dispreferred_record = (
                candidates.get(row.dispreferred_candidate_id)
                if row.dispreferred_candidate_id
                else None
            )
            preferred = self._features(
                row.preferred_text,
                self._record_components(preferred_record),
            )
            dispreferred = self._features(
                row.dispreferred_text,
                self._record_components(dispreferred_record),
            )
            result.append((preferred, dispreferred))
        return result

    @staticmethod
    def _fit(rows: list[tuple[np.ndarray, np.ndarray]]) -> np.ndarray:
        weights = np.zeros(len(FEATURE_NAMES), dtype=np.float64)
        learning_rate = 0.08
        regularization = 0.002
        for _epoch in range(120):
            for preferred, dispreferred in rows:
                difference = preferred - dispreferred
                probability = PairwiseCaptionPreferenceRanker._sigmoid(
                    float(np.dot(weights, difference))
                )
                gradient = (1.0 - probability) * difference - regularization * weights
                weights += learning_rate * gradient
            learning_rate *= 0.985
        return weights

    @staticmethod
    def _features(text: str, components: dict[str, float]) -> np.ndarray:
        structure = analyze_caption(text).structure
        words = max(1, len(text.split()))
        values = {
            "bias": 1.0,
            "open_question": float(structure == "open_question"),
            "yes_no_question": float(structure == "yes_no_question"),
            "observation": float(structure == "observation"),
            "reaction": float(structure == "reaction"),
            "explanation": float(structure == "explanation"),
            "promotional": float(structure == "promotional_statement"),
            "length_log": math.log1p(words) / math.log(30),
            "grounding": components.get("grounding", 0.5),
            "policy": components.get("policy", 0.5),
            "style": components.get("style", 0.5),
            "novelty": components.get("novelty", 0.5),
            "rotation": components.get("rotation", 0.5),
            "positive_feedback": components.get("positive_feedback", 0.0),
            "negative_feedback_risk": -components.get("negative_feedback_risk", 0.0),
            "pairing": components.get("pairing", 0.5),
        }
        return np.asarray([values[name] for name in FEATURE_NAMES], dtype=np.float64)

    @staticmethod
    def _record_components(
        record: CaptionCandidateRecord | None,
    ) -> dict[str, float]:
        if record is None:
            return {}
        return {
            "grounding": record.grounding_score,
            "policy": record.policy_score,
            "style": record.style_score,
            "novelty": record.novelty_score,
            "rotation": record.rotation_score,
            "positive_feedback": record.positive_feedback_score,
            "negative_feedback_risk": record.negative_feedback_risk,
            "pairing": record.pairing_score,
        }

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

    @staticmethod
    def _sigmoid(value: float) -> float:
        clipped = max(-30.0, min(30.0, value))
        return 1.0 / (1.0 + math.exp(-clipped))


class DeterministicImageCaptionPairRanker:
    def score_pair(
        self,
        *,
        image_score: float,
        caption_score: float,
        grounding_score: float,
    ) -> PreferenceScore:
        value = (
            0.35 * image_score + 0.35 * caption_score + 0.30 * grounding_score
        )
        return PreferenceScore(
            score=max(0.0, min(1.0, value)),
            trained=False,
            calibrated=False,
            reason="deterministic image-caption compatibility baseline",
            sample_count=0,
        )
