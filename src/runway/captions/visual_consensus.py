from __future__ import annotations

import json
import re
from collections.abc import Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from runway.analysis.schemas import (
    CandidateAnalysis,
    CandidateFieldConfidence,
    VisualEntity,
)

FactField = Literal[
    "entity",
    "object",
    "action",
    "relationship",
    "emotion",
    "scene",
    "setting",
    "composition",
    "ocr",
]

_STOP_WORDS = {
    "a",
    "an",
    "and",
    "appears",
    "are",
    "at",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
}
_ALIASES = {
    "annoyed": "annoy",
    "bored": "bore",
    "container": "packet",
    "crackers": "cracker",
    "eating": "eat",
    "eats": "eat",
    "held": "hold",
    "holding": "hold",
    "holds": "hold",
    "indoors": "indoor",
    "package": "packet",
    "packaging": "packet",
    "reading": "read",
    "reads": "read",
    "seated": "sit",
    "sitting": "sit",
    "sofa": "couch",
    "wrapping": "wrapper",
}
_GENERIC_ACTIONS = {
    "argue": "arguing",
    "drive": "driving",
    "eat": "eating",
    "hold": "holding something",
    "kiss": "kissing",
    "look": "looking",
    "read": "reading",
    "run": "running",
    "sit": "sitting",
    "sleep": "sleeping",
    "talk": "talking",
    "watch": "watching",
    "write": "writing",
}


class ConsensusFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: FactField
    primary_value: str
    audit_value: str
    similarity: float = Field(ge=0, le=1)
    confidence: float = Field(ge=0, le=1)


class DisputedFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: FactField
    value: str
    reason: str


class VisualConsensusReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    analysis: CandidateAnalysis
    confirmed_facts: list[ConsensusFact]
    disputed_facts: list[DisputedFact]
    trusted_source_evidence: dict[str, object]
    primary_confidence: float = Field(ge=0, le=1)
    audit_confidence: float = Field(ge=0, le=1)
    consensus_coverage: float = Field(ge=0, le=1)


def candidate_analysis_from_mapping(payload: dict[str, Any]) -> CandidateAnalysis:
    """Normalize legacy or partially populated analysis rows into the canonical schema."""

    confidence = _bounded_float(payload.get("confidence"), default=0.0)
    raw_entities = payload.get("entities")
    entities: list[VisualEntity] = []
    if isinstance(raw_entities, list):
        for raw in raw_entities:
            if not isinstance(raw, dict) or not str(raw.get("name") or "").strip():
                continue
            name = str(raw["name"]).strip()
            entities.append(
                VisualEntity(
                    name=name,
                    entity_type=str(raw.get("entity_type") or "unknown"),
                    confidence=_bounded_float(raw.get("confidence"), default=confidence),
                    canonical_name=(
                        str(raw["canonical_name"]).strip()
                        if raw.get("canonical_name") not in (None, "")
                        else None
                    ),
                )
            )
    if not entities:
        for character in _string_list(payload.get("characters")):
            entities.append(
                VisualEntity(
                    name=character,
                    entity_type="fictional_character",
                    confidence=confidence,
                    canonical_name=character,
                )
            )
    raw_field_confidence = payload.get("field_confidence")
    field_confidence = raw_field_confidence if isinstance(raw_field_confidence, dict) else {}
    return CandidateAnalysis(
        franchise=(
            str(payload["franchise"]).strip()
            if payload.get("franchise") not in (None, "")
            else None
        ),
        characters=_string_list(payload.get("characters"))
        or [entity.canonical_name or entity.name for entity in entities],
        scene_archetype=str(payload.get("scene_archetype") or "unknown"),
        composition=str(payload.get("composition") or "unknown"),
        emotion=str(payload.get("emotion") or "unknown"),
        text_overlay=bool(payload.get("text_overlay", False)),
        watermark_probability=_bounded_float(payload.get("watermark_probability")),
        unsafe_probability=_bounded_float(payload.get("unsafe_probability")),
        personal_artwork_probability=_bounded_float(payload.get("personal_artwork_probability")),
        fan_art_probability=_bounded_float(payload.get("fan_art_probability")),
        caption_potential=_bounded_float(payload.get("caption_potential"), default=0.5),
        confidence=confidence,
        entities=entities,
        objects=_string_list(payload.get("objects")),
        actions=_string_list(payload.get("actions")),
        relationships=_string_list(payload.get("relationships")),
        setting=str(payload.get("setting") or "unknown"),
        ocr_text=_string_list(payload.get("ocr_text")),
        field_confidence=CandidateFieldConfidence(
            entities=_bounded_float(field_confidence.get("entities"), default=confidence),
            emotion=_bounded_float(field_confidence.get("emotion"), default=confidence),
            actions=_bounded_float(field_confidence.get("actions"), default=confidence),
            scene=_bounded_float(field_confidence.get("scene"), default=confidence),
            ocr=_bounded_float(field_confidence.get("ocr"), default=confidence),
        ),
    )


def trusted_source_evidence(
    *,
    source_domain: str | None,
    provider_result_json: str,
) -> dict[str, object]:
    """Return only source evidence whose adapter ties it to the exact candidate frame."""

    domain = (source_domain or "").casefold().removeprefix("www.")
    if domain != "frinkiac.com":
        return {}
    try:
        payload: object = json.loads(provider_result_json)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}
    metadata = payload.get("provider_metadata")
    if not isinstance(metadata, dict) or metadata.get("source_adapter") != "frinkiac-public-search":
        return {}
    selected = {
        key: metadata[key]
        for key in ("episode", "timestamp", "subtitle", "episode_title")
        if metadata.get(key) not in (None, "")
    }
    if not selected:
        return {}
    return {
        "source": "frinkiac",
        "trust": "frame_aligned_source_metadata",
        **selected,
    }


def reconcile_visual_analyses(
    primary: CandidateAnalysis,
    audit: CandidateAnalysis,
    *,
    source_evidence: dict[str, object] | None = None,
) -> VisualConsensusReport:
    confirmed: list[ConsensusFact] = []
    disputed: list[DisputedFact] = []

    primary_entities = primary.entities
    audit_entities = audit.entities
    entities, entity_confirmed, entity_disputed = _reconcile_entities(
        primary_entities,
        audit_entities,
    )
    confirmed.extend(entity_confirmed)
    disputed.extend(entity_disputed)

    objects, rows, misses = _reconcile_values(
        "object",
        primary.objects,
        audit.objects,
        min(primary.confidence, audit.confidence),
    )
    confirmed.extend(rows)
    disputed.extend(misses)
    actions, rows, misses = _reconcile_values(
        "action",
        primary.actions,
        audit.actions,
        min(primary.field_confidence.actions, audit.field_confidence.actions),
        threshold=0.5,
    )
    confirmed.extend(rows)
    disputed.extend(misses)
    relationships, rows, misses = _reconcile_values(
        "relationship",
        primary.relationships,
        audit.relationships,
        min(primary.confidence, audit.confidence),
    )
    confirmed.extend(rows)
    disputed.extend(misses)
    ocr_text, rows, misses = _reconcile_values(
        "ocr",
        primary.ocr_text,
        audit.ocr_text,
        min(primary.field_confidence.ocr, audit.field_confidence.ocr),
        threshold=0.9,
    )
    confirmed.extend(rows)
    disputed.extend(misses)

    emotion, row, miss = _reconcile_scalar(
        "emotion",
        primary.emotion,
        audit.emotion,
        min(primary.field_confidence.emotion, audit.field_confidence.emotion),
    )
    _append_optional(confirmed, disputed, row, miss)
    scene, row, miss = _reconcile_scalar(
        "scene",
        primary.scene_archetype,
        audit.scene_archetype,
        min(primary.field_confidence.scene, audit.field_confidence.scene),
        threshold=0.45,
    )
    _append_optional(confirmed, disputed, row, miss)
    setting, row, miss = _reconcile_scalar(
        "setting",
        primary.setting,
        audit.setting,
        min(primary.field_confidence.scene, audit.field_confidence.scene),
        threshold=0.4,
    )
    _append_optional(confirmed, disputed, row, miss)
    composition, row, miss = _reconcile_scalar(
        "composition",
        primary.composition,
        audit.composition,
        min(primary.field_confidence.scene, audit.field_confidence.scene),
        threshold=0.35,
    )
    _append_optional(confirmed, disputed, row, miss)

    franchise = (
        primary.franchise
        if _similarity(primary.franchise or "", audit.franchise or "") >= 0.8
        else None
    )
    if primary.franchise and franchise is None:
        disputed.append(
            DisputedFact(
                field="entity",
                value=primary.franchise,
                reason="independent visual audit did not confirm the franchise",
            )
        )
    characters = sorted(
        {
            entity.canonical_name or entity.name
            for entity in entities
            if entity.entity_type.casefold() in {"fictional character", "fictional_character"}
        }
    )
    primary_fact_count = max(
        1,
        len(primary.entities)
        + len(primary.objects)
        + len(primary.actions)
        + len(primary.relationships)
        + len(primary.ocr_text)
        + 4,
    )
    coverage = min(1.0, len(confirmed) / primary_fact_count)
    consensus_confidence = min(primary.confidence, audit.confidence, 0.55 + 0.45 * coverage)
    analysis = CandidateAnalysis(
        franchise=franchise,
        characters=characters,
        scene_archetype=scene,
        composition=composition,
        emotion=emotion,
        text_overlay=primary.text_overlay and audit.text_overlay,
        watermark_probability=max(primary.watermark_probability, audit.watermark_probability),
        unsafe_probability=max(primary.unsafe_probability, audit.unsafe_probability),
        personal_artwork_probability=max(
            primary.personal_artwork_probability,
            audit.personal_artwork_probability,
        ),
        fan_art_probability=max(primary.fan_art_probability, audit.fan_art_probability),
        caption_potential=min(primary.caption_potential, audit.caption_potential),
        confidence=consensus_confidence,
        entities=entities,
        objects=objects,
        actions=actions,
        relationships=relationships,
        setting=setting,
        ocr_text=ocr_text,
        field_confidence=CandidateFieldConfidence(
            entities=min(
                primary.field_confidence.entities,
                audit.field_confidence.entities,
                consensus_confidence,
            ),
            emotion=(
                min(primary.field_confidence.emotion, audit.field_confidence.emotion)
                if emotion != "unknown"
                else 0.0
            ),
            actions=(
                min(primary.field_confidence.actions, audit.field_confidence.actions)
                if actions
                else 0.0
            ),
            scene=(
                min(primary.field_confidence.scene, audit.field_confidence.scene)
                if scene != "unknown"
                else 0.0
            ),
            ocr=(
                min(primary.field_confidence.ocr, audit.field_confidence.ocr) if ocr_text else 0.0
            ),
        ),
    )
    return VisualConsensusReport(
        analysis=analysis,
        confirmed_facts=confirmed,
        disputed_facts=disputed,
        trusted_source_evidence=source_evidence or {},
        primary_confidence=primary.confidence,
        audit_confidence=audit.confidence,
        consensus_coverage=coverage,
    )


def reconcile_visual_audit_sequence(
    primary: CandidateAnalysis,
    audits: Sequence[CandidateAnalysis],
    *,
    source_evidence: dict[str, object] | None = None,
) -> VisualConsensusReport:
    """Intersect visual facts across independent audits and preserve every disagreement."""

    if not audits:
        raise ValueError("at least one independent visual audit is required")
    report = reconcile_visual_analyses(
        primary,
        audits[0],
        source_evidence=source_evidence,
    )
    coverages = [report.consensus_coverage]
    disputes = list(report.disputed_facts)
    for audit in audits[1:]:
        report = reconcile_visual_analyses(
            report.analysis,
            audit,
            source_evidence=source_evidence,
        )
        coverages.append(report.consensus_coverage)
        disputes.extend(report.disputed_facts)
    deduplicated_disputes: list[DisputedFact] = []
    seen: set[tuple[str, str, str]] = set()
    for dispute in disputes:
        key = (dispute.field, dispute.value.casefold(), dispute.reason.casefold())
        if key in seen:
            continue
        seen.add(key)
        deduplicated_disputes.append(dispute)
    return report.model_copy(
        update={
            "disputed_facts": deduplicated_disputes,
            "primary_confidence": primary.confidence,
            "audit_confidence": min(audit.confidence for audit in audits),
            "consensus_coverage": min(coverages),
        }
    )


def _reconcile_entities(
    primary: list[VisualEntity],
    audit: list[VisualEntity],
) -> tuple[list[VisualEntity], list[ConsensusFact], list[DisputedFact]]:
    result: list[VisualEntity] = []
    confirmed: list[ConsensusFact] = []
    disputed: list[DisputedFact] = []
    for value in primary:
        primary_name = value.canonical_name or value.name
        match, score = _best_match(
            primary_name,
            [candidate.canonical_name or candidate.name for candidate in audit],
        )
        if match is None or score < 0.75:
            disputed.append(
                DisputedFact(
                    field="entity",
                    value=primary_name,
                    reason="independent visual audit did not confirm the entity",
                )
            )
            continue
        audit_entity = next(
            candidate
            for candidate in audit
            if (candidate.canonical_name or candidate.name) == match
        )
        confidence = min(value.confidence, audit_entity.confidence)
        result.append(value.model_copy(update={"confidence": confidence}))
        confirmed.append(
            ConsensusFact(
                field="entity",
                primary_value=primary_name,
                audit_value=match,
                similarity=score,
                confidence=confidence,
            )
        )
    return result, confirmed, disputed


def _reconcile_values(
    field: FactField,
    primary: list[str],
    audit: list[str],
    confidence: float,
    *,
    threshold: float = 0.58,
) -> tuple[list[str], list[ConsensusFact], list[DisputedFact]]:
    values: list[str] = []
    confirmed: list[ConsensusFact] = []
    disputed: list[DisputedFact] = []
    for value in primary:
        match, score = _best_match(value, audit)
        if match is None or score < threshold:
            generic_action = (
                _shared_generic_action(value, match)
                if field == "action" and match is not None
                else None
            )
            if generic_action is not None:
                values.append(generic_action)
                confirmed.append(
                    ConsensusFact(
                        field=field,
                        primary_value=value,
                        audit_value=match,
                        similarity=max(score, 0.5),
                        confidence=confidence,
                    )
                )
                if _has_specific_action_detail(value):
                    disputed.append(
                        DisputedFact(
                            field=field,
                            value=value,
                            reason=(
                                "only the generic action was independently confirmed; "
                                "its specific object or target was not"
                            ),
                        )
                    )
                continue
            disputed.append(
                DisputedFact(
                    field=field,
                    value=value,
                    reason="independent visual audit did not confirm this fact",
                )
            )
            continue
        values.append(value)
        confirmed.append(
            ConsensusFact(
                field=field,
                primary_value=value,
                audit_value=match,
                similarity=score,
                confidence=confidence,
            )
        )
    return values, confirmed, disputed


def _reconcile_scalar(
    field: FactField,
    primary: str,
    audit: str,
    confidence: float,
    *,
    threshold: float = 0.5,
) -> tuple[str, ConsensusFact | None, DisputedFact | None]:
    if _is_unknown(primary):
        return "unknown", None, None
    if _is_unknown(audit) or _similarity(primary, audit) < threshold:
        return (
            "unknown",
            None,
            DisputedFact(
                field=field,
                value=primary or "unknown",
                reason="independent visual audit disagreed or lacked sufficient evidence",
            ),
        )
    score = _similarity(primary, audit)
    return (
        primary,
        ConsensusFact(
            field=field,
            primary_value=primary,
            audit_value=audit,
            similarity=score,
            confidence=confidence,
        ),
        None,
    )


def _append_optional(
    confirmed: list[ConsensusFact],
    disputed: list[DisputedFact],
    row: ConsensusFact | None,
    miss: DisputedFact | None,
) -> None:
    if row is not None:
        confirmed.append(row)
    if miss is not None:
        disputed.append(miss)


def _best_match(value: str, candidates: list[str]) -> tuple[str | None, float]:
    if not candidates:
        return None, 0.0
    scored = sorted(
        ((_similarity(value, candidate), candidate) for candidate in candidates),
        key=lambda item: (-item[0], item[1]),
    )
    score, candidate = scored[0]
    return candidate, score


def _similarity(first: str, second: str) -> float:
    first_tokens = _tokens(first)
    second_tokens = _tokens(second)
    if not first_tokens or not second_tokens:
        return 0.0
    if first_tokens == second_tokens:
        return 1.0
    if first_tokens <= second_tokens or second_tokens <= first_tokens:
        return min(len(first_tokens), len(second_tokens)) / max(
            len(first_tokens),
            len(second_tokens),
        )
    return len(first_tokens & second_tokens) / len(first_tokens | second_tokens)


def _tokens(value: str) -> set[str]:
    result: set[str] = set()
    for raw in re.findall(r"[a-z0-9']+", value.casefold()):
        if raw in _STOP_WORDS:
            continue
        token = _ALIASES.get(raw, raw)
        if token.endswith("s") and len(token) > 4:
            token = token[:-1]
        result.add(token)
    return result


def _shared_generic_action(first: str, second: str | None) -> str | None:
    if second is None:
        return None
    shared = _tokens(first) & _tokens(second) & set(_GENERIC_ACTIONS)
    if len(shared) != 1:
        return None
    return _GENERIC_ACTIONS[next(iter(shared))]


def _has_specific_action_detail(value: str) -> bool:
    tokens = _tokens(value)
    action_tokens = tokens & set(_GENERIC_ACTIONS)
    generic_fillers = {"item", "object", "person", "someone", "something"}
    return bool(tokens - action_tokens - generic_fillers)


def _is_unknown(value: str) -> bool:
    return value.strip().casefold() in {"", "n/a", "none", "unknown", "unclear"}


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _bounded_float(value: object, *, default: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return max(0.0, min(1.0, float(value)))
