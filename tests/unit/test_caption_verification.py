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
from runway.captions.service import CaptionService
from runway.captions.verification import CaptionVerifier, is_generic_engagement_bait
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


def test_two_grounded_caption_candidates_form_a_usable_slate() -> None:
    service = object.__new__(CaptionService)

    assert service._has_minimum_eligible_slate(
        [
            {"eligible": True},
            {"eligible": True},
            {"eligible": False},
        ]
    )
    assert not service._has_minimum_eligible_slate(
        [
            {"eligible": True},
            {"eligible": False},
        ]
    )


def test_question_first_slate_requires_a_grounded_open_question() -> None:
    service = object.__new__(CaptionService)
    policy = PolicySnapshot(
        channel_id=1,
        version="question-first-test",
        rules=[],
        preferred_structures=["open_question", "observation"],
        prohibited_claims=[],
        language="en",
        locale="en-CA",
        rights_policy="provenance_only",
        source_policy="public_web_nsfw_blocked",
        question_first=True,
    )

    def row(text: str, structure: str) -> dict[str, object]:
        return {
            "eligible": True,
            "candidate": CaptionCandidate(
                text=text,
                structure=structure,  # type: ignore[arg-type]
                language="en",
                editorial_angle="test",
                visible_evidence=[],
                uncertainty=[],
                historical_evidence=[],
                feedback_evidence=[],
                confidence=0.9,
            ),
        }

    observations = [
        row("The Simpsons delivers courtroom drama", "observation"),
        row("A clenched fist takes centre stage", "observation"),
    ]
    with_question = [
        row("What sparked that raised fist?", "open_question"),
        observations[0],
    ]

    assert (
        service._slate_shortfall_reason(observations, policy)
        == "no grounded open-ended question survived for a question-first channel"
    )
    assert service._slate_shortfall_reason(with_question, policy) is None


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


def test_delight_emotion_has_one_happy_canonical_group() -> None:
    assert CaptionVerifier._canonical_emotion("delight") == "happy"
    assert CaptionVerifier._canonical_emotion("delighted") == "happy"
    assert CaptionVerifier._canonical_emotion("happiness") == "happy"


@pytest.mark.parametrize(
    "text",
    [
        "What is Homer thinking about?",
        "What is happening here?",
        "What's going on here?",
        "What stands out first here?",
        "Which detail catches your eye first?",
        "Which detail feels most mysterious here?",
        "What detail seems unusual?",
        "What detail did you spot first?",
        "Why is he standing there?",
        "How are they waiting here?",
        "What is she doing?",
        "What are the characters doing there?",
        "Which face contrasts most with raised fists?",
        "Which detail makes Homer look unusual?",
        "What part makes her seem different?",
        "Why is Homer looking right?",
        "Why is she looking away?",
        "Why is he holding that receiver?",
        "Why is Bart holding the purple object?",
        "What is happening at that podium?",
        "What's going on near the table?",
    ],
)
def test_generic_open_questions_fail_policy(text: str) -> None:
    candidate = CaptionCandidate(
        text=text,
        structure="open_question",
        language="en",
        editorial_angle="audience_inquiry",
        visible_evidence=["Homer"],
        uncertainty=[],
        historical_evidence=[],
        feedback_evidence=[],
        confidence=0.9,
    )

    result = CaptionVerifier().verify(candidate, _brief())  # type: ignore[arg-type]

    assert result.passed is False
    assert "policy:generic_engagement_bait" in result.unsupported_claims


def test_visually_specific_pronoun_question_is_not_treated_as_generic() -> None:
    assert not is_generic_engagement_bait("Why is he standing alone in these snowy mountains?")


def test_incomplete_bare_verb_question_fails_policy() -> None:
    candidate = CaptionCandidate(
        text="Why keep one hand on the receiver?",
        structure="open_question",
        language="en",
        editorial_angle="audience_inquiry",
        visible_evidence=["telephone receiver"],
        uncertainty=[],
        historical_evidence=[],
        feedback_evidence=[],
        confidence=0.9,
    )

    result = CaptionVerifier().verify(candidate, _brief())  # type: ignore[arg-type]

    assert result.passed is False
    assert "policy:incomplete_question" in result.unsupported_claims


def test_clinical_generic_figure_label_fails_policy() -> None:
    candidate = CaptionCandidate(
        text="Why are some figures seated while others stand?",
        structure="open_question",
        language="en",
        editorial_angle="audience_inquiry",
        visible_evidence=["seated and standing characters"],
        uncertainty=[],
        historical_evidence=[],
        feedback_evidence=[],
        confidence=0.9,
    )

    result = CaptionVerifier().verify(candidate, _brief())  # type: ignore[arg-type]

    assert result.passed is False
    assert "policy:clinical_subject_label:figure" in result.unsupported_claims


def test_literal_action_figure_is_not_treated_as_a_clinical_subject_label() -> None:
    candidate = CaptionCandidate(
        text="Which action figure would you choose?",
        structure="open_question",
        language="en",
        editorial_angle="audience_inquiry",
        visible_evidence=["action figure"],
        uncertainty=[],
        historical_evidence=[],
        feedback_evidence=[],
        confidence=0.9,
    )

    result = CaptionVerifier().verify(candidate, _brief())  # type: ignore[arg-type]

    assert "policy:clinical_subject_label:figure" not in result.unsupported_claims


@pytest.mark.parametrize(
    "text",
    [
        "Why is the left character covering his ears?",
        "What is the person on the right holding?",
        "Why is the foreground figure waving?",
        "What surprised the background character?",
    ],
)
def test_spatial_subject_label_fails_policy(text: str) -> None:
    candidate = CaptionCandidate(
        text=text,
        structure="open_question",
        language="en",
        editorial_angle="audience_inquiry",
        visible_evidence=["two characters"],
        uncertainty=[],
        historical_evidence=[],
        feedback_evidence=[],
        confidence=0.9,
    )

    result = CaptionVerifier().verify(candidate, _brief())  # type: ignore[arg-type]

    assert result.passed is False
    assert "policy:clinical_subject_label:spatial" in result.unsupported_claims


@pytest.mark.parametrize(
    ("text", "failure"),
    [
        ("Why is that spherical object foregrounded?", "policy:composition_jargon"),
        ("A tense two-shot in formalwear.", "policy:composition_jargon"),
        ("A circular object sits nearby.", "policy:clinical_object_label:shape"),
        ("A very unusual control room.", "policy:generic_adjective_filler"),
        ("An interesting room.", "policy:generic_adjective_filler"),
        ("This frame has serious contrast.", "policy:generic_contrast_filler"),
        ("The frame shows strong contrast.", "policy:generic_contrast_filler"),
    ],
)
def test_analysis_jargon_and_empty_filler_fail_policy(
    text: str,
    failure: str,
) -> None:
    candidate = CaptionCandidate(
        text=text,
        structure="open_question" if text.endswith("?") else "observation",
        language="en",
        editorial_angle="audience_inquiry",
        visible_evidence=["control room"],
        uncertainty=[],
        historical_evidence=[],
        feedback_evidence=[],
        confidence=0.9,
    )

    result = CaptionVerifier().verify(candidate, _brief())  # type: ignore[arg-type]

    assert result.passed is False
    assert failure in result.unsupported_claims


def test_ambiguous_object_name_must_keep_its_visual_modifier() -> None:
    brief = _brief().model_copy(
        update={
            "uncertain_facts": [
                EditorialFact(
                    field="object",
                    value="two champagne flutes",
                    confidence=0.65,
                    evidence="independent_visual_consensus",
                )
            ]
        }
    )

    ambiguous = CaptionVerifier().verify(
        CaptionCandidate(
            text="Which flute catches your eye first?",
            structure="open_question",
            language="en",
            editorial_angle="audience_inquiry",
            visible_evidence=["two champagne flutes"],
            uncertainty=[],
            historical_evidence=[],
            feedback_evidence=[],
            confidence=0.9,
        ),
        brief,
    )
    precise = CaptionVerifier().verify(
        CaptionCandidate(
            text="Which champagne flute catches your eye?",
            structure="open_question",
            language="en",
            editorial_angle="audience_inquiry",
            visible_evidence=["two champagne flutes"],
            uncertainty=[],
            historical_evidence=[],
            feedback_evidence=[],
            confidence=0.9,
        ),
        brief,
    )

    assert ambiguous.passed is False
    assert "policy:ambiguous_object_label:flute" in ambiguous.unsupported_claims
    assert precise.passed is True


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


@pytest.mark.parametrize(
    "text",
    [
        "Would you trust this look?",
        "Which look wins?",
        "Who would you choose?",
    ],
)
def test_question_openers_are_not_misclassified_as_named_entities(text: str) -> None:
    candidate = CaptionCandidate(
        text=text,
        structure="open_question",
        language="en",
        editorial_angle="audience_inquiry",
        visible_evidence=["Homer", "smiling"],
        uncertainty=[],
        historical_evidence=[],
        feedback_evidence=[],
        confidence=0.9,
    )

    result = CaptionVerifier().verify(candidate, _brief())  # type: ignore[arg-type]

    assert result.passed is True
    assert not any(value.startswith("unsupported_entity:") for value in result.unsupported_claims)


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


@pytest.mark.parametrize(
    ("caption", "unknown_answer"),
    [
        ("What sparked that raised fist?", "What caused or sparked the raised fist is not shown."),
        ("What follows that clenched fist?", "What follows the fist is not shown."),
        ("Why raise a fist here?", "The reason for raising it is not visible."),
        ("How did that fist become the focus?", "The cause is not known."),
        (
            "Why are Wiggum and Krusty facing each other?",
            "The reason they are facing each other.",
        ),
    ],
)
def test_open_question_answer_wording_from_live_verifier_is_normalized(
    caption: str,
    unknown_answer: str,
) -> None:
    assessment = CaptionGroundingAssessment(
        candidate_id=1,
        verdict="uncertain",
        grounding_score=0.6,
        factual_claims=["A raised clenched fist is visible."],
        supported_claims=["A raised clenched fist is visible."],
        uncertain_claims=[unknown_answer],
        unsupported_claims=[],
        visual_evidence=["A suited character visibly holds one fist raised."],
        source_evidence=[],
        contradictions=[],
        corrected_caption=None,
        confidence=0.97,
    )

    normalized = normalize_open_question_answer_uncertainty(caption, assessment)

    assert normalized.verdict == "supported"
    assert normalized.grounding_score == 0.97
    assert normalized.uncertain_claims == []


def test_subjective_question_unknown_answer_does_not_invalidate_visible_premise() -> None:
    assessment = CaptionGroundingAssessment(
        candidate_id=1,
        verdict="uncertain",
        grounding_score=0.6,
        factual_claims=["A person is holding playing cards."],
        supported_claims=["A person is holding a hand of playing cards."],
        uncertain_claims=["Whether the hand is trustworthy."],
        unsupported_claims=[],
        visual_evidence=["Several playing cards are visible in one person's hand."],
        source_evidence=[],
        contradictions=[],
        corrected_caption=None,
        confidence=0.94,
    )

    normalized = normalize_open_question_answer_uncertainty(
        "Would you trust that hand?",
        assessment,
    )

    assert normalized.verdict == "supported"
    assert normalized.grounding_score == 0.94
    assert normalized.uncertain_claims == []


def test_subjective_question_does_not_hide_uncertain_action_premise() -> None:
    assessment = CaptionGroundingAssessment(
        candidate_id=1,
        verdict="uncertain",
        grounding_score=0.6,
        factual_claims=["Homer is reading."],
        supported_claims=["Homer is visible."],
        uncertain_claims=["The image does not establish whether Homer is reading."],
        unsupported_claims=[],
        visual_evidence=["Homer holds an object but is not visibly reading it."],
        source_evidence=[],
        contradictions=[],
        corrected_caption="Would you trust Homer with that object?",
        confidence=0.97,
    )

    normalized = normalize_open_question_answer_uncertainty(
        "Would you trust Homer reading this?",
        assessment,
    )

    assert normalized is assessment
    assert normalized.verdict == "uncertain"


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


def test_completed_caption_contract_accepts_one_strong_alternative() -> None:
    options = CaptionOptions(
        recommended="Why is Fry gripping that pipe?",
        alternatives=["Fry and a glowing robot share the frame."],
        rationale="Both captions survived independent grounding.",
        confidence=0.91,
        referenced_historical_post_ids=[],
        factual_uncertainty_warning=None,
    )

    assert [options.recommended, *options.alternatives] == [
        "Why is Fry gripping that pipe?",
        "Fry and a glowing robot share the frame.",
    ]


def test_completed_caption_contract_rejects_a_single_caption() -> None:
    with pytest.raises(ValueError, match="requires at least one alternative"):
        CaptionOptions(
            recommended="Why is Fry gripping that pipe?",
            alternatives=[],
            rationale="Only one caption survived.",
            confidence=0.91,
            referenced_historical_post_ids=[],
            factual_uncertainty_warning=None,
        )
