from __future__ import annotations

import json

from runway.analysis.schemas import CandidateAnalysis
from runway.captions.visual_consensus import (
    candidate_analysis_from_mapping,
    reconcile_visual_analyses,
    reconcile_visual_audit_sequence,
    trusted_source_evidence,
)


def _analysis(*, objects: list[str], actions: list[str]) -> CandidateAnalysis:
    return candidate_analysis_from_mapping(
        {
            "franchise": "The Simpsons",
            "characters": ["Homer Simpson"],
            "scene_archetype": "relaxing at home",
            "composition": "Homer is seated on a couch",
            "emotion": "content",
            "confidence": 0.96,
            "entities": [
                {
                    "name": "Homer Simpson",
                    "canonical_name": "Homer Simpson",
                    "entity_type": "fictional_character",
                    "confidence": 0.99,
                }
            ],
            "objects": objects,
            "actions": actions,
            "setting": "living room",
            "field_confidence": {
                "entities": 0.99,
                "emotion": 0.85,
                "actions": 0.95,
                "scene": 0.95,
                "ocr": 0.99,
            },
        }
    )


def test_visual_consensus_removes_wrong_object_and_correlated_action() -> None:
    primary = _analysis(
        objects=["newspaper", "couch"],
        actions=["holding a newspaper", "sitting"],
    )
    independent = _analysis(
        objects=["cracker wrapper", "couch"],
        actions=["holding a cracker wrapper", "sitting"],
    )

    result = reconcile_visual_analyses(primary, independent)

    assert result.analysis.objects == ["couch"]
    assert result.analysis.actions == ["holding something", "sitting"]
    assert {row.value for row in result.disputed_facts} >= {
        "newspaper",
        "holding a newspaper",
    }
    assert "holding something" not in {row.value for row in result.disputed_facts}
    assert "sitting" not in {row.value for row in result.disputed_facts}


def test_unknown_intermediate_scalars_are_not_recorded_as_disputed_facts() -> None:
    primary = _analysis(objects=["couch"], actions=["sitting"])
    first_audit = primary.model_copy(
        update={"emotion": "unknown", "scene_archetype": "unknown", "setting": "unknown"}
    )
    second_audit = primary.model_copy(deep=True)

    result = reconcile_visual_audit_sequence(primary, [first_audit, second_audit])

    assert "unknown" not in {row.value for row in result.disputed_facts}


def test_later_blind_audit_breaks_correlated_first_pass_error() -> None:
    primary = _analysis(
        objects=["newspaper", "couch"],
        actions=["holding a newspaper", "sitting"],
    )
    agreeing_but_wrong_audit = _analysis(
        objects=["newspaper", "couch"],
        actions=["holding a newspaper", "sitting"],
    )
    detail_audit = _analysis(
        objects=["cracker package", "couch"],
        actions=["holding an open cracker package", "sitting"],
    )

    result = reconcile_visual_audit_sequence(
        primary,
        [agreeing_but_wrong_audit, detail_audit],
    )

    assert result.analysis.objects == ["couch"]
    assert result.analysis.actions == ["holding something", "sitting"]
    assert {row.value for row in result.disputed_facts} >= {
        "newspaper",
        "holding a newspaper",
    }


def test_only_frame_aligned_source_metadata_is_promoted() -> None:
    provider_result = json.dumps(
        {
            "provider_metadata": {
                "source_adapter": "frinkiac-public-search",
                "episode": "S37E15",
                "timestamp": 231439,
                "subtitle": "Marge won't let me eat crackers in bed",
                "episode_title": "Homer? A Cracker Bro?",
                "search_query": "newspaper",
            }
        }
    )

    trusted = trusted_source_evidence(
        source_domain="frinkiac.com",
        provider_result_json=provider_result,
    )

    assert trusted["subtitle"] == "Marge won't let me eat crackers in bed"
    assert "search_query" not in trusted
    assert (
        trusted_source_evidence(
            source_domain="random.example",
            provider_result_json=provider_result,
        )
        == {}
    )


def test_legacy_partial_analysis_is_normalized_without_inventing_facts() -> None:
    result = candidate_analysis_from_mapping(
        {
            "characters": ["Homer Simpson"],
            "emotion": "excited",
            "confidence": 0.8,
        }
    )

    assert result.characters == ["Homer Simpson"]
    assert result.entities[0].canonical_name == "Homer Simpson"
    assert result.objects == []
    assert result.actions == []
