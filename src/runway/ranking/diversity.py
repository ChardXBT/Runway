from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from runway.analysis.schemas import CandidateAnalysis
from runway.db.models import CandidateImage


def _normalized(value: object) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", str(value).casefold()).split())


def _family(text: str, families: dict[str, set[str]], fallback: str) -> str:
    words = set(_normalized(text).split())
    for name, markers in families.items():
        if words.intersection(markers):
            return name
    return fallback


SCENE_FAMILIES = {
    "vehicle": {"car", "drive", "driving", "road", "truck", "vehicle", "wheel"},
    "meal": {"breakfast", "dinner", "drink", "eating", "food", "kitchen", "lunch"},
    "work_school": {"class", "desk", "office", "school", "work"},
    "celebration": {"celebration", "dance", "party", "wedding"},
    "conflict": {"argument", "chase", "conflict", "fight"},
    "performance": {"concert", "microphone", "performance", "stage"},
    "outdoors": {"beach", "forest", "garden", "outside", "park"},
    "home": {"bedroom", "couch", "house", "living", "room"},
}
EMOTION_FAMILIES = {
    "shock_fear": {
        "afraid",
        "alarm",
        "fear",
        "panic",
        "scared",
        "shock",
        "shocked",
        "surprise",
        "surprised",
        "worried",
    },
    "joy_excitement": {
        "delighted",
        "excited",
        "happy",
        "joy",
        "laughing",
        "smiling",
    },
    "anger": {"angry", "annoyed", "frustrated", "furious"},
    "sadness": {"crying", "dejected", "sad", "upset"},
    "confusion": {"confused", "curious", "puzzled", "skeptical"},
    "calm": {"calm", "confident", "determined", "neutral"},
}
COMPOSITION_FAMILIES = {
    "close_up": {"close", "closeup", "portrait", "tight"},
    "two_shot": {"couple", "pair", "two", "twoshot"},
    "group": {"crowd", "ensemble", "family", "group", "multiple"},
    "wide": {"establishing", "full", "landscape", "wide"},
    "action": {"action", "dynamic", "motion"},
}


@dataclass(frozen=True)
class DiversityFingerprint:
    franchise: str
    scene_family: str
    setting_family: str
    emotion_family: str
    composition_family: str
    characters: tuple[str, ...]
    actions: tuple[str, ...]
    query_tokens: tuple[str, ...]

    @property
    def cluster_key(self) -> str:
        broad = "|".join(
            (
                self.franchise,
                self.scene_family,
                self.setting_family,
                self.emotion_family,
                self.composition_family,
            )
        )
        return f"dv3-{hashlib.sha256(broad.encode('utf-8')).hexdigest()[:20]}"

    @property
    def concept_key(self) -> str:
        return "|".join(
            (
                self.franchise,
                self.scene_family,
                self.setting_family,
                self.emotion_family,
            )
        )

    def model_dump(self) -> dict[str, object]:
        return {
            "version": "candidate-diversity-v3",
            "franchise": self.franchise,
            "scene_family": self.scene_family,
            "setting_family": self.setting_family,
            "emotion_family": self.emotion_family,
            "composition_family": self.composition_family,
            "characters": list(self.characters),
            "actions": list(self.actions),
            "query_tokens": list(self.query_tokens),
            "cluster_key": self.cluster_key,
            "concept_key": self.concept_key,
        }


def build_diversity_fingerprint(
    analysis: CandidateAnalysis,
    search_query: str,
) -> DiversityFingerprint:
    scene_text = " ".join(
        (
            analysis.scene_archetype,
            analysis.setting,
            analysis.composition,
            " ".join(analysis.actions),
            search_query,
        )
    )
    setting_text = " ".join(
        (
            analysis.setting,
            analysis.scene_archetype,
            analysis.composition,
            search_query,
        )
    )
    emotion_text = " ".join((analysis.emotion, analysis.scene_archetype))
    composition_text = " ".join((analysis.composition, search_query))
    query_tokens = tuple(
        sorted(
            token
            for token in set(_normalized(search_query).split())
            if len(token) > 2
            and token
            not in {
                "animated",
                "character",
                "frame",
                "image",
                "scene",
                "show",
                "still",
            }
        )
    )
    return DiversityFingerprint(
        franchise=_normalized(analysis.franchise or "unknown"),
        scene_family=_family(scene_text, SCENE_FAMILIES, _normalized(analysis.scene_archetype)),
        setting_family=_family(setting_text, SCENE_FAMILIES, _normalized(analysis.setting)),
        emotion_family=_family(emotion_text, EMOTION_FAMILIES, _normalized(analysis.emotion)),
        composition_family=_family(
            composition_text,
            COMPOSITION_FAMILIES,
            _normalized(analysis.composition),
        ),
        characters=tuple(
            sorted({_normalized(value) for value in analysis.characters if _normalized(value)})
        ),
        actions=tuple(
            sorted({_normalized(value) for value in analysis.actions if _normalized(value)})
        ),
        query_tokens=query_tokens,
    )


def candidate_analysis(candidate: CandidateImage) -> CandidateAnalysis:
    raw_analysis: object = json.loads(candidate.detected_topic_json)
    if not isinstance(raw_analysis, dict):
        raise ValueError(f"candidate {candidate.id} has invalid analysis metadata")
    compatible = {
        **raw_analysis,
        "franchise": raw_analysis.get("franchise"),
        "characters": raw_analysis.get("characters", []),
        "scene_archetype": raw_analysis.get("scene_archetype", "unknown"),
        "composition": raw_analysis.get("composition", "unknown"),
        "emotion": raw_analysis.get("emotion", "unknown"),
        "text_overlay": raw_analysis.get("text_overlay", False),
        "watermark_probability": raw_analysis.get("watermark_probability", 0.0),
        "unsafe_probability": raw_analysis.get("unsafe_probability", 0.0),
        "personal_artwork_probability": raw_analysis.get(
            "personal_artwork_probability",
            0.0,
        ),
        "fan_art_probability": raw_analysis.get("fan_art_probability", 0.0),
        "caption_potential": raw_analysis.get("caption_potential", 0.5),
        "confidence": raw_analysis.get("confidence", 0.0),
        "entities": raw_analysis.get("entities", []),
        "objects": raw_analysis.get("objects", []),
        "actions": raw_analysis.get("actions", []),
        "relationships": raw_analysis.get("relationships", []),
        "setting": raw_analysis.get("setting", "unknown"),
        "ocr_text": raw_analysis.get("ocr_text", []),
        "field_confidence": {
            key: (
                raw_analysis.get("field_confidence", {}).get(key, 0.0)
                if isinstance(raw_analysis.get("field_confidence"), dict)
                else 0.0
            )
            for key in ("entities", "emotion", "actions", "scene", "ocr")
        },
    }
    return CandidateAnalysis.model_validate(compatible)


def fingerprint_from_candidate(candidate: CandidateImage) -> DiversityFingerprint:
    stored: dict[str, Any]
    try:
        raw = json.loads(candidate.diversity_fingerprint_json or "{}")
        stored = raw if isinstance(raw, dict) else {}
    except json.JSONDecodeError:
        stored = {}
    if stored.get("version") == "candidate-diversity-v3":
        return DiversityFingerprint(
            franchise=str(stored.get("franchise") or "unknown"),
            scene_family=str(stored.get("scene_family") or "unknown"),
            setting_family=str(stored.get("setting_family") or "unknown"),
            emotion_family=str(stored.get("emotion_family") or "unknown"),
            composition_family=str(stored.get("composition_family") or "unknown"),
            characters=tuple(str(value) for value in stored.get("characters", [])),
            actions=tuple(str(value) for value in stored.get("actions", [])),
            query_tokens=tuple(str(value) for value in stored.get("query_tokens", [])),
        )
    return build_diversity_fingerprint(candidate_analysis(candidate), candidate.search_query)


def _jaccard(left: Iterable[str], right: Iterable[str]) -> float:
    left_set = set(left)
    right_set = set(right)
    if not left_set or not right_set:
        return 0.0
    return len(left_set.intersection(right_set)) / len(left_set.union(right_set))


def fingerprint_similarity(
    left: DiversityFingerprint,
    right: DiversityFingerprint,
) -> float:
    dimensions = (
        (left.franchise, right.franchise, 0.08),
        (left.scene_family, right.scene_family, 0.25),
        (left.setting_family, right.setting_family, 0.16),
        (left.emotion_family, right.emotion_family, 0.22),
        (left.composition_family, right.composition_family, 0.14),
    )
    score = sum(weight for first, second, weight in dimensions if first == second)
    score += 0.08 * _jaccard(left.characters, right.characters)
    score += 0.04 * _jaccard(left.actions, right.actions)
    score += 0.03 * _jaccard(left.query_tokens, right.query_tokens)
    return min(1.0, score)


class CandidateDiversitySelector:
    """Deterministic set-level ranking that suppresses repetitive candidate clusters."""

    def __init__(self, *, relevance_weight: float = 0.66):
        if not 0 < relevance_weight < 1:
            raise ValueError("relevance_weight must be between zero and one")
        self.relevance_weight = relevance_weight

    def order(
        self,
        candidates: Sequence[CandidateImage],
        *,
        anchors: Sequence[CandidateImage] = (),
        max_similarity: float | None = 0.88,
    ) -> list[CandidateImage]:
        remaining = list(candidates)
        selected: list[CandidateImage] = []
        anchor_fingerprints = [fingerprint_from_candidate(value) for value in anchors]
        fingerprints = {value.id: fingerprint_from_candidate(value) for value in remaining}
        while remaining:
            reference_fingerprints = [
                *anchor_fingerprints,
                *(fingerprints[value.id] for value in selected),
            ]
            eligible = [
                candidate
                for candidate in remaining
                if max_similarity is None
                or not reference_fingerprints
                or (
                    all(
                        fingerprints[candidate.id].concept_key != reference.concept_key
                        for reference in reference_fingerprints
                    )
                    and max(
                        fingerprint_similarity(
                            fingerprints[candidate.id],
                            reference,
                        )
                        for reference in reference_fingerprints
                    )
                    < max_similarity
                )
            ]
            if not eligible:
                break
            best = max(
                eligible,
                key=lambda candidate: (
                    self._selection_score(
                        candidate,
                        fingerprints[candidate.id],
                        reference_fingerprints,
                    ),
                    candidate.final_rank_score,
                    -candidate.id,
                ),
            )
            selected.append(best)
            remaining.remove(best)
        return selected

    def _selection_score(
        self,
        candidate: CandidateImage,
        fingerprint: DiversityFingerprint,
        selected: Sequence[DiversityFingerprint],
    ) -> float:
        max_similarity = max(
            (fingerprint_similarity(fingerprint, other) for other in selected),
            default=0.0,
        )
        exact_cluster_repeat = any(
            fingerprint.cluster_key == other.cluster_key for other in selected
        )
        repeat_penalty = 0.2 if exact_cluster_repeat else 0.0
        return (
            self.relevance_weight * candidate.final_rank_score
            - (1 - self.relevance_weight) * max_similarity
            - repeat_penalty
        )


def assign_diversity_fields(
    candidate: CandidateImage,
    analysis: CandidateAnalysis | None = None,
) -> DiversityFingerprint:
    fingerprint = (
        build_diversity_fingerprint(analysis, candidate.search_query)
        if analysis is not None
        else fingerprint_from_candidate(candidate)
    )
    candidate.diversity_cluster_key = fingerprint.cluster_key
    candidate.diversity_fingerprint_json = json.dumps(
        fingerprint.model_dump(),
        sort_keys=True,
    )
    return fingerprint
