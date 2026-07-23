from __future__ import annotations

import pytest

from runway.analysis.schemas import (
    CaptionCandidate,
    CaptionGroundingAssessment,
    CaptionOptions,
)
from runway.captions.claim_grounding import (
    ClaimLevelGroundingVerifier,
    normalize_open_question_answer_uncertainty,
)
from runway.captions.planning import EditorialBrief, EditorialFact, EditorialPlanner
from runway.captions.verification import CaptionVerifier
from runway.intelligence.embeddings import configuration_hash
from runway.intelligence.policies import PolicySnapshot


def _brief() -> EditorialBrief:
    policy = PolicySnapshot(
        channel_id=1,
        version=configuration_hash({"fixture": "grounding"}),
        rules=[],
        preferred_structures=["open_question", "observation", "reaction"],
        prohibited_claims=[
            "unsupported_entity",
            "invented_event",
            "invented_quote",
            "unsupported_relationship",
        ],
        language="en",
        locale="en-CA",
        rights_policy="unknown_requires_review",
        source_policy="preserve_and_review",
        question_first=True,
    )
    return EditorialPlanner().build(
        candidate_analysis={
            "entities": [
                {
                    "name": "Homer",
                    "canonical_name": "Homer",
                    "entity_type": "character",
                    "confidence": 0.99,
                }
            ],
            "actions": ["smiling"],
            "emotion": "excited",
            "scene_archetype": "reaction",
            "confidence": 0.99,
            "field_confidence": {
                "entities": 0.99,
                "actions": 0.99,
                "emotion": 0.99,
                "scene": 0.99,
            },
        },
        retrieval_context={
            "retrieval_run_id": 1,
            "style_profile": {"caption_statistics": {"word_percentiles": {"p25": 2, "p75": 16}}},
            "caption_style_examples": [],
            "feedback_context": {},
            "negative_examples": [],
            "rotation_state": {},
            "explicit_rules": [],
        },
        policy=policy,
        source_context={"rights_status": "creator_owned"},
    )


@pytest.mark.parametrize(
    ("text", "expected_failure"),
    [
        ("Why is Bart smiling?", "unsupported_entity:Bart"),
        ("Why is Homer smiling after the episode?", "invented_event:episode"),
        ('Why did Homer say "I won"?', "invented_quote:I won"),
        ("Why is Homer's brother smiling?", "unsupported_relationship:brother"),
    ],
)
def test_unsupported_critical_claims_fail(
    text: str,
    expected_failure: str,
) -> None:
    candidate = CaptionCandidate(
        text=text,
        structure="open_question",
        language="en",
        editorial_angle="visible_reaction",
        visible_evidence=["Homer", "smiling"],
        uncertainty=[],
        historical_evidence=[],
        feedback_evidence=[],
        confidence=0.8,
    )

    result = CaptionVerifier().verify(candidate, _brief())  # type: ignore[arg-type]

    assert result.passed is False
    assert expected_failure in result.unsupported_claims


def test_low_confidence_entity_cannot_become_verified_fact() -> None:
    policy = PolicySnapshot(
        channel_id=1,
        version="fixture",
        rules=[],
        preferred_structures=["observation"],
        prohibited_claims=["unsupported_entity"],
        language="en",
        locale="en-CA",
        rights_policy="unknown_requires_review",
        source_policy="preserve_and_review",
        question_first=False,
    )
    brief = EditorialPlanner().build(
        candidate_analysis={
            "entities": [
                {
                    "name": "Maybe Bart",
                    "canonical_name": "Bart",
                    "entity_type": "character",
                    "confidence": 0.41,
                }
            ],
            "actions": ["looking"],
            "confidence": 0.9,
            "field_confidence": {"actions": 0.9},
        },
        retrieval_context={
            "retrieval_run_id": 1,
            "style_profile": {"caption_statistics": {}},
        },
        policy=policy,
        source_context={"rights_status": "creator_owned"},
    )
    result = CaptionVerifier().verify(
        CaptionCandidate(
            text="Bart is looking.",
            structure="observation",
            language="en",
            editorial_angle="visual_observation",
            visible_evidence=[],
            uncertainty=[],
            historical_evidence=[],
            feedback_evidence=[],
            confidence=0.8,
        ),
        brief,
    )
    assert "Bart" not in brief.supported_entity_names
    assert "unsupported_entity:Bart" in result.unsupported_claims
    assert result.passed is False


def test_question_presupposition_requires_visible_action() -> None:
    candidate = CaptionCandidate(
        text="What is Homer reading?",
        structure="open_question",
        language="en",
        editorial_angle="audience_inquiry",
        visible_evidence=["Homer", "Homer is holding a piece of paper"],
        uncertainty=[],
        historical_evidence=[],
        feedback_evidence=[],
        confidence=0.94,
    )

    result = CaptionVerifier().verify(candidate, _brief())  # type: ignore[arg-type]

    assert result.passed is False
    assert "unsupported_action:reading" in result.unsupported_claims


def test_independent_visual_verdict_cannot_be_overridden_by_fast_layer() -> None:
    candidate = CaptionCandidate(
        text="Why is Homer smiling?",
        structure="open_question",
        language="en",
        editorial_angle="audience_inquiry",
        visible_evidence=["Homer", "smiling"],
        uncertainty=[],
        historical_evidence=[],
        feedback_evidence=[],
        confidence=0.9,
    )
    brief = _brief()
    fast = CaptionVerifier().verify(candidate, brief)  # type: ignore[arg-type]
    assert fast.passed is True
    semantic = CaptionGroundingAssessment(
        candidate_id=1,
        verdict="unsupported",
        grounding_score=0.05,
        factual_claims=["Homer is visible", "Homer is smiling"],
        supported_claims=["Homer is visible"],
        uncertain_claims=[],
        unsupported_claims=["Homer is smiling"],
        visual_evidence=["mouth and expression do not show a smile"],
        source_evidence=[],
        contradictions=[],
        corrected_caption="What is Homer holding?",
        confidence=0.96,
    )

    result = ClaimLevelGroundingVerifier().verify(
        caption=candidate.text,
        brief=brief,  # type: ignore[arg-type]
        first_layer=fast,
        image_path="unused-by-precomputed-semantic-assessment",
        semantic_assessment=semantic,
    )

    assert result.passed is False
    assert result.semantic_layer_status == "independent_frontier_visual_verifier_completed"
    assert result.critical_failures == ["Homer is smiling"]
    classifications = {claim.claim: claim.classification for claim in result.claims}
    assert classifications == {
        "Homer is visible": "visible",
        "Homer is smiling": "unsupported",
    }


def test_grounding_schema_downgrades_self_inconsistent_supported_verdict() -> None:
    result = CaptionGroundingAssessment(
        candidate_id=1,
        verdict="supported",
        grounding_score=0.93,
        factual_claims=["Homer is reading"],
        supported_claims=["Homer is visible"],
        uncertain_claims=[],
        unsupported_claims=[],
        visual_evidence=["Homer holds paper"],
        source_evidence=[],
        contradictions=["holding paper does not establish reading"],
        corrected_caption="What is Homer holding?",
        confidence=0.94,
    )

    assert result.verdict == "unsupported"
    assert result.grounding_score == 0.2


def test_grounding_schema_rejects_empty_supported_approval() -> None:
    with pytest.raises(ValueError, match="at least 1 item"):
        CaptionGroundingAssessment(
            candidate_id=1,
            verdict="supported",
            grounding_score=0.99,
            factual_claims=[],
            supported_claims=[],
            uncertain_claims=[],
            unsupported_claims=[],
            visual_evidence=[],
            source_evidence=[],
            contradictions=[],
            corrected_caption=None,
            confidence=0.99,
        )


def test_confirmed_generic_action_survives_disputed_specific_object() -> None:
    assert CaptionVerifier._evidence_supported(
        "Homer holding something",
        ["Homer", "holding something"],
        ["holding a newspaper"],
    )
    assert not CaptionVerifier._evidence_supported(
        "Homer holding a newspaper",
        ["Homer", "holding something"],
        ["holding a newspaper"],
    )


def test_candidate_evidence_normalizes_emotion_synonyms() -> None:
    assert CaptionVerifier._evidence_supported(
        "determined",
        ["determination"],
        [],
    )


def test_private_generator_evidence_mismatch_is_audited_without_vetoing_caption() -> None:
    candidate = CaptionCandidate(
        text="Why is Homer smiling?",
        structure="open_question",
        language="en",
        editorial_angle="audience_inquiry",
        visible_evidence=["Homer", "smiling", "Homer is wearing a crown"],
        uncertainty=[],
        historical_evidence=[],
        feedback_evidence=[],
        confidence=0.9,
    )

    result = CaptionVerifier().verify(candidate, _brief())  # type: ignore[arg-type]

    assert result.passed is True
    assert result.unsupported_claims == []
    assert result.warnings[-1] == ("unverified_candidate_evidence:Homer is wearing a crown")


def test_open_question_unknown_answer_does_not_invalidate_supported_premise() -> None:
    assessment = CaptionGroundingAssessment(
        candidate_id=1,
        verdict="uncertain",
        grounding_score=0.6,
        factual_claims=["Homer is holding something."],
        supported_claims=["Homer is visibly holding an object."],
        uncertain_claims=["The exact identity of the object is left open by the question."],
        unsupported_claims=[],
        visual_evidence=["Homer visibly grasps a folded paper."],
        source_evidence=[],
        contradictions=[],
        corrected_caption=None,
        confidence=0.96,
    )

    normalized = normalize_open_question_answer_uncertainty(
        "What is Homer holding?",
        assessment,
    )

    assert normalized.verdict == "supported"
    assert normalized.grounding_score == 0.96
    assert normalized.uncertain_claims == []


def test_open_question_material_premise_uncertainty_still_fails() -> None:
    assessment = CaptionGroundingAssessment(
        candidate_id=1,
        verdict="uncertain",
        grounding_score=0.6,
        factual_claims=["Homer is reading."],
        supported_claims=["Homer is holding paper."],
        uncertain_claims=["The image does not establish whether Homer is actually reading."],
        unsupported_claims=[],
        visual_evidence=["Homer holds paper but is not looking at it."],
        source_evidence=[],
        contradictions=[],
        corrected_caption="What is Homer holding?",
        confidence=0.98,
    )

    normalized = normalize_open_question_answer_uncertainty(
        "What is Homer reading?",
        assessment,
    )

    assert normalized is assessment
    assert normalized.verdict == "uncertain"


def test_disputed_specific_object_cannot_leak_into_caption() -> None:
    brief = _brief()
    brief = brief.model_copy(
        update={
            "visible_facts": [
                *brief.visible_facts,
                EditorialFact(
                    field="action",
                    value="holding something",
                    confidence=0.9,
                    evidence="independent_visual_consensus",
                ),
            ],
            "uncertain_facts": [
                *brief.uncertain_facts,
                EditorialFact(
                    field="object",
                    value="newspaper",
                    confidence=0.0,
                    evidence="independent_visual_audit_disagreement",
                ),
            ],
        }
    )
    candidate = CaptionCandidate(
        text="Homer holds the paper.",
        structure="observation",
        language="en",
        editorial_angle="visual_observation",
        visible_evidence=["Homer", "holding something"],
        uncertainty=[],
        historical_evidence=[],
        feedback_evidence=[],
        confidence=0.9,
    )

    result = CaptionVerifier().verify(candidate, brief)

    assert result.passed is False
    assert "unsupported_disputed_fact:object:newspaper" in result.unsupported_claims


def test_abstention_contract_cannot_contain_filler_captions() -> None:
    options = CaptionOptions(
        recommended="",
        alternatives=[],
        rationale="No grounded option survived.",
        confidence=0.0,
        referenced_historical_post_ids=[],
        factual_uncertainty_warning="No grounded caption.",
        abstained=True,
        abstention_reason="grounding failure",
    )
    assert options.abstained is True
    assert options.recommended == ""
    assert options.alternatives == []
