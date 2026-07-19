from __future__ import annotations

import hashlib
import json
import math

from runway.captions.taxonomy import analyze_caption
from runway.db.models import CaptionCandidateRecord

FEATURE_SCHEMA_VERSION = "caption-preference-features-v1"
TAXONOMY_VERSION = "caption-taxonomy-v1"
VERIFIER_VERSION = "caption-verifier-v1"

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


def candidate_components(
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


def caption_feature_values(
    text: str,
    components: dict[str, float],
) -> dict[str, float]:
    structure = analyze_caption(text).structure
    words = max(1, len(text.split()))
    return {
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
        "negative_feedback_risk": -components.get(
            "negative_feedback_risk",
            0.0,
        ),
        "pairing": components.get("pairing", 0.5),
    }


def feature_snapshot(
    text: str,
    components: dict[str, float],
    *,
    context: dict[str, object] | None = None,
) -> dict[str, object]:
    values = caption_feature_values(text, components)
    return {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "taxonomy_version": TAXONOMY_VERSION,
        "verifier_version": VERIFIER_VERSION,
        "text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "features": {name: values[name] for name in FEATURE_NAMES},
        "context": context or {},
    }


def snapshot_json_and_hash(
    text: str,
    components: dict[str, float],
    *,
    context: dict[str, object] | None = None,
) -> tuple[str, str]:
    payload = json.dumps(
        feature_snapshot(text, components, context=context),
        sort_keys=True,
        separators=(",", ":"),
    )
    return payload, hashlib.sha256(payload.encode("utf-8")).hexdigest()


def feature_vector(snapshot: dict[str, object]) -> list[float]:
    values = snapshot.get("features")
    if not isinstance(values, dict):
        raise ValueError("feature snapshot has no feature mapping")
    result: list[float] = []
    for name in FEATURE_NAMES:
        value = values.get(name)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"feature snapshot has invalid {name!r}")
        result.append(float(value))
    return result
