from __future__ import annotations

import json

from runway.analysis.schemas import CandidateAnalysis
from runway.db.models import CandidateImage
from runway.intelligence.candidate_diversity import CandidateDiversityService
from runway.intelligence.slate_optimization import CandidateSlateOptimizer
from runway.ranking.diversity import (
    CandidateDiversitySelector,
    assign_diversity_fields,
    build_diversity_fingerprint,
    fingerprint_from_candidate,
    fingerprint_similarity,
)


def _analysis(
    *,
    scene: str,
    setting: str,
    emotion: str,
    composition: str,
    characters: list[str],
    actions: list[str],
) -> CandidateAnalysis:
    return CandidateAnalysis(
        franchise="The Simpsons",
        characters=characters,
        scene_archetype=scene,
        composition=composition,
        emotion=emotion,
        text_overlay=False,
        watermark_probability=0.0,
        unsafe_probability=0.0,
        personal_artwork_probability=0.0,
        fan_art_probability=0.0,
        caption_potential=0.8,
        confidence=0.9,
        entities=[],
        objects=[],
        actions=actions,
        relationships=[],
        setting=setting,
        ocr_text=[],
        field_confidence={
            "entities": 0.9,
            "emotion": 0.9,
            "actions": 0.9,
            "scene": 0.9,
            "ocr": 0.9,
        },
    )


def _candidate(
    candidate_id: int,
    score: float,
    query: str,
    analysis: CandidateAnalysis,
) -> CandidateImage:
    candidate = CandidateImage(
        id=candidate_id,
        search_run_id=1,
        media_asset_id=candidate_id,
        search_query=query,
        result_rank=candidate_id,
        provider_result_json="{}",
        detected_topic_json=analysis.model_dump_json(),
        quality_score=score,
        style_score=score,
        novelty_score=score,
        caption_potential_score=score,
        source_risk_score=0.0,
        final_rank_score=score,
        soft_warnings_json="[]",
        score_components_json="{}",
        selection_reason="fixture",
        diversity_fingerprint_json="{}",
    )
    assign_diversity_fields(candidate, analysis)
    return candidate


def test_vehicle_panic_scenes_are_clustered_as_highly_similar() -> None:
    driving = build_diversity_fingerprint(
        _analysis(
            scene="family panicking in a car",
            setting="road inside a car",
            emotion="shock and fear",
            composition="group shot",
            characters=["Homer", "Marge"],
            actions=["driving", "panicking"],
        ),
        "Simpsons family shocked driving scene",
    )
    passenger = build_diversity_fingerprint(
        _analysis(
            scene="reaction in a vehicle",
            setting="inside a car",
            emotion="surprised",
            composition="group close up",
            characters=["Homer", "Bart"],
            actions=["reacting", "driving"],
        ),
        "Homer Bart car panic frame",
    )

    assert driving.scene_family == "vehicle"
    assert passenger.scene_family == "vehicle"
    assert driving.emotion_family == passenger.emotion_family == "shock_fear"
    assert fingerprint_similarity(driving, passenger) >= 0.7


def test_selector_prefers_a_distinct_scene_over_a_near_duplicate_with_higher_score() -> None:
    vehicle = _analysis(
        scene="panic in a car",
        setting="inside a vehicle",
        emotion="shocked",
        composition="group shot",
        characters=["Homer", "Marge"],
        actions=["driving"],
    )
    meal = _analysis(
        scene="family eating dinner",
        setting="kitchen table",
        emotion="happy",
        composition="wide family shot",
        characters=["Homer", "Lisa"],
        actions=["eating"],
    )
    candidates = [
        _candidate(1, 0.95, "Simpsons car panic", vehicle),
        _candidate(2, 0.94, "Homer shocked while driving", vehicle),
        _candidate(3, 0.82, "Simpsons family dinner discussion", meal),
    ]

    ordered = CandidateDiversitySelector().order(candidates)

    assert [candidate.id for candidate in ordered[:2]] == [1, 3]
    assert json.loads(ordered[0].diversity_fingerprint_json)["version"] == (
        "candidate-diversity-v3"
    )


def test_legacy_candidate_analysis_is_compatible_with_cluster_backfill() -> None:
    candidate = CandidateImage(
        id=9,
        search_run_id=1,
        media_asset_id=9,
        search_query="Simpsons shocked in a car",
        result_rank=1,
        provider_result_json="{}",
        detected_topic_json=json.dumps(
            {
                "franchise": "The Simpsons",
                "characters": ["Homer"],
                "scene_archetype": "driving reaction",
                "composition": "close up",
                "emotion": "shock",
                "text_overlay": False,
                "watermark_probability": 0.0,
                "unsafe_probability": 0.0,
                "personal_artwork_probability": 0.0,
                "fan_art_probability": 0.0,
                "caption_potential": 0.8,
                "confidence": 0.9,
            }
        ),
        quality_score=0.8,
        style_score=0.8,
        novelty_score=0.8,
        caption_potential_score=0.8,
        source_risk_score=0.0,
        final_rank_score=0.8,
        soft_warnings_json="[]",
        score_components_json="{}",
        selection_reason="legacy",
        diversity_fingerprint_json="{}",
    )

    fingerprint = fingerprint_from_candidate(candidate)

    assert fingerprint.scene_family == "vehicle"
    assert fingerprint.emotion_family == "shock_fear"


def test_rights_warnings_are_removed_without_erasing_other_quality_warnings() -> None:
    assert CandidateDiversityService._without_rights_warnings(
        [
            "rights status is unknown and requires human review",
            "possible fan art",
            "possible watermark",
            "historical date precision cannot prove the 180-day boundary",
        ]
    ) == ["historical date precision cannot prove the 180-day boundary"]


def test_slate_optimizer_treats_two_frames_from_one_archive_episode_as_repetitive() -> None:
    analysis = _analysis(
        scene="two distinct moments",
        setting="different rooms",
        emotion="calm",
        composition="wide",
        characters=["Homer"],
        actions=["standing"],
    )
    first = _candidate(21, 0.9, "first", analysis)
    second = _candidate(22, 0.8, "second", analysis)
    first.provider_result_json = json.dumps(
        {
            "provider_metadata": {
                "source_adapter": "frinkiac-public-search",
                "episode": "S18E11",
                "timestamp": 1000,
            }
        }
    )
    second.provider_result_json = json.dumps(
        {
            "provider_metadata": {
                "source_adapter": "frinkiac-public-search",
                "episode": "S18E11",
                "timestamp": 90_000,
            }
        }
    )

    assert CandidateSlateOptimizer._shares_archive_episode(second, [first])


def test_slate_optimizer_suppresses_repeated_joined_hand_actions_within_slate() -> None:
    clasped = _candidate(
        31,
        0.9,
        "Simpsons stage gesture",
        _analysis(
            scene="stage presentation",
            setting="theatre",
            emotion="excited",
            composition="close up",
            characters=["cartoon woman"],
            actions=["clasping hands"],
        ),
    )
    holding = _candidate(
        32,
        0.88,
        "Futurama outdoor group",
        _analysis(
            scene="outdoor group interaction",
            setting="grassy park",
            emotion="cheerful",
            composition="wide",
            characters=["cartoon children"],
            actions=["holding hands"],
        ),
    )
    raised = _candidate(
        33,
        0.86,
        "Bender in a forest",
        _analysis(
            scene="forest reaction",
            setting="forest",
            emotion="curious",
            composition="close up",
            characters=["Bender"],
            actions=["raising one hand"],
        ),
    )
    fingerprints = {row.id: fingerprint_from_candidate(row) for row in (clasped, holding, raised)}

    assert CandidateSlateOptimizer._shares_action_family(
        fingerprints[holding.id],
        [clasped],
        fingerprints,
    )
    assert not CandidateSlateOptimizer._shares_action_family(
        fingerprints[raised.id],
        [clasped],
        fingerprints,
    )


def test_primary_franchise_quota_recovers_recent_mix_deficit_before_rotation() -> None:
    primary_analysis = _analysis(
        scene="varied scene",
        setting="varied setting",
        emotion="calm",
        composition="wide",
        characters=["Homer"],
        actions=["standing"],
    )
    selected = [
        _candidate(index, 0.9, f"Simpsons option {index}", primary_analysis)
        for index in range(100, 119)
    ]
    fingerprints = {row.id: fingerprint_from_candidate(row) for row in selected}
    history = ["the simpsons"] * 38 + ["futurama"] * 12 + ["family guy"] * 8 + ["unknown"] * 7

    assert CandidateSlateOptimizer._primary_quota_required(
        primary_franchise="the simpsons",
        minimum_share=0.67,
        history=history,
        selected=selected[:18],
        fingerprints=fingerprints,
    )
    assert not CandidateSlateOptimizer._primary_quota_required(
        primary_franchise="the simpsons",
        minimum_share=0.67,
        history=history,
        selected=selected,
        fingerprints=fingerprints,
    )


def test_primary_franchise_matching_is_case_and_whitespace_insensitive() -> None:
    assert CandidateSlateOptimizer._is_primary_franchise(
        "  The   Simpsons ",
        "the simpsons",
    )
    assert not CandidateSlateOptimizer._is_primary_franchise(
        "Futurama",
        "the simpsons",
    )
