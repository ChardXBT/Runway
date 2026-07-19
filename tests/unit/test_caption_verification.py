from __future__ import annotations

import pytest

from runway.analysis.schemas import CaptionCandidate, CaptionOptions
from runway.captions.planning import EditorialPlanner
from runway.captions.verification import CaptionVerifier
from runway.intelligence.embeddings import configuration_hash
from runway.intelligence.policies import PolicySnapshot


def _brief() -> object:
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
