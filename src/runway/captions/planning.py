from __future__ import annotations

from typing import Any, cast

from pydantic import BaseModel, ConfigDict, Field

from runway.intelligence.policies import PolicySnapshot


def _required_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{field} must be an integer")
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an integer") from exc


class EditorialFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    value: str
    confidence: float = Field(ge=0, le=1)
    evidence: str


class EditorialBrief(BaseModel):
    model_config = ConfigDict(extra="forbid")

    visible_facts: list[EditorialFact]
    uncertain_facts: list[EditorialFact]
    prohibited_claims: list[str]
    supported_entity_names: list[str]
    recent_rotation: dict[str, object]
    content_mode: dict[str, object]
    possible_editorial_angles: list[str]
    recommended_angles: list[str]
    target_structures: list[str]
    target_language: str
    target_locale: str
    target_length: dict[str, int]
    positive_evidence: list[dict[str, object]]
    negative_evidence: list[dict[str, object]]
    explicit_rules: list[dict[str, object]]
    source_context: dict[str, object]
    rights_context: dict[str, object]
    policy_version: str
    retrieval_run_id: int


class EditorialPlanner:
    verified_threshold = 0.75

    def build(
        self,
        *,
        candidate_analysis: dict[str, Any],
        retrieval_context: dict[str, object],
        policy: PolicySnapshot,
        source_context: dict[str, object],
    ) -> EditorialBrief:
        confidence_map = candidate_analysis.get("field_confidence", {})
        if not isinstance(confidence_map, dict):
            confidence_map = {}
        global_confidence = float(candidate_analysis.get("confidence") or 0.0)
        facts: list[EditorialFact] = []
        uncertain: list[EditorialFact] = []

        def append_fact(field: str, value: object, confidence_key: str) -> None:
            text = str(value or "").strip()
            if not text or text.casefold() in {"unknown", "none", "null"}:
                return
            raw_confidence = confidence_map.get(confidence_key, global_confidence)
            confidence = (
                float(raw_confidence)
                if isinstance(raw_confidence, (int, float))
                else global_confidence
            )
            fact = EditorialFact(
                field=field,
                value=text,
                confidence=max(0.0, min(1.0, confidence)),
                evidence=f"candidate_analysis.{field}",
            )
            (facts if fact.confidence >= self.verified_threshold else uncertain).append(fact)

        for entity in candidate_analysis.get("entities", []):
            if not isinstance(entity, dict):
                continue
            name = entity.get("canonical_name") or entity.get("name")
            if not name:
                continue
            confidence = float(entity.get("confidence") or global_confidence)
            fact = EditorialFact(
                field="entity",
                value=str(name),
                confidence=max(0.0, min(1.0, confidence)),
                evidence="candidate_analysis.entities",
            )
            (facts if fact.confidence >= self.verified_threshold else uncertain).append(fact)
        if not any(fact.field == "entity" for fact in facts + uncertain):
            for character in candidate_analysis.get("characters", []):
                append_fact("entity", character, "entities")
        for value in candidate_analysis.get("objects", []):
            append_fact("object", value, "objects")
        for value in candidate_analysis.get("actions", []):
            append_fact("action", value, "actions")
        for value in candidate_analysis.get("relationships", []):
            append_fact("relationship", value, "relationships")
        for value in candidate_analysis.get("ocr_text", []):
            append_fact("ocr", value, "ocr")
        append_fact("emotion", candidate_analysis.get("emotion"), "emotion")
        append_fact("scene", candidate_analysis.get("scene_archetype"), "scene")
        append_fact("setting", candidate_analysis.get("setting"), "scene")
        append_fact("composition", candidate_analysis.get("composition"), "composition")

        supported_entities = sorted({fact.value for fact in facts if fact.field == "entity"})
        profile = cast(
            dict[str, object],
            retrieval_context.get("style_profile", {}),
        )
        stats = cast(dict[str, Any], profile.get("caption_statistics", {}))
        percentiles = cast(dict[str, Any], stats.get("word_percentiles", {}))
        minimum = max(1, round(float(percentiles.get("p25", 3))))
        maximum = max(minimum, round(float(percentiles.get("p75", 12))))
        positive = cast(
            list[dict[str, object]],
            retrieval_context.get("caption_style_examples", []),
        )[:8]
        feedback = cast(
            dict[str, object],
            retrieval_context.get("feedback_context", {}),
        )
        positive.extend(cast(list[dict[str, object]], feedback.get("positive_examples", []))[:6])
        negative = cast(
            list[dict[str, object]],
            retrieval_context.get("negative_examples", []),
        )[:8]
        negative.extend(cast(list[dict[str, object]], feedback.get("negative_examples", []))[:6])
        visible_fields = {fact.field for fact in facts}
        angles = ["visual_observation"]
        if "emotion" in visible_fields:
            angles.append("visible_reaction")
        if "action" in visible_fields:
            angles.append("visible_action")
        if "relationship" in visible_fields:
            angles.append("visible_relationship")
        if "object" in visible_fields:
            angles.append("object_focus")
        if policy.question_first:
            angles.insert(0, "audience_inquiry")
        preferred_angles = angles[:3]
        mode_scores = cast(
            list[dict[str, object]],
            profile.get("candidate_mode_scores", []),
        )
        modes_payload = cast(dict[str, object], profile.get("content_modes", {}))
        learned_modes = cast(list[dict[str, object]], modes_payload.get("modes", []))
        selected_mode: dict[str, object] = {"status": "unavailable"}
        if mode_scores:
            leading_mode_id = str(mode_scores[0].get("mode_id") or "")
            learned = next(
                (
                    mode
                    for mode in learned_modes
                    if str(mode.get("mode_id") or "") == leading_mode_id
                ),
                None,
            )
            selected_mode = {
                "status": "selected",
                "mode_id": leading_mode_id,
                "fit_score": mode_scores[0].get("score", 0.0),
                "prototype": learned or {},
                "candidate_scores": mode_scores,
            }
        return EditorialBrief(
            visible_facts=facts,
            uncertain_facts=uncertain,
            prohibited_claims=policy.prohibited_claims,
            supported_entity_names=supported_entities,
            recent_rotation=cast(
                dict[str, object],
                retrieval_context.get("rotation_state", {}),
            ),
            content_mode=selected_mode,
            possible_editorial_angles=angles,
            recommended_angles=preferred_angles,
            target_structures=policy.preferred_structures,
            target_language=policy.language,
            target_locale=policy.locale,
            target_length={"minimum_words": minimum, "maximum_words": maximum},
            positive_evidence=positive[:12],
            negative_evidence=negative[:12],
            explicit_rules=cast(
                list[dict[str, object]],
                retrieval_context.get("explicit_rules", []),
            ),
            source_context=source_context,
            rights_context={
                "status": source_context.get("rights_status", "unknown"),
                "policy": policy.rights_policy,
            },
            policy_version=policy.version,
            retrieval_run_id=_required_int(
                retrieval_context["retrieval_run_id"],
                "retrieval_run_id",
            ),
        )
