from __future__ import annotations

import json
from pathlib import Path

import pytest
from sqlalchemy import select

from runway.analysis.schemas import (
    CandidateAnalysis,
    CandidateFieldConfidence,
    SearchPlan,
    SearchQueryFamily,
    VisualEntity,
)
from runway.analysis.service import AnalysisService
from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ChannelPolicyRule, MediaAsset, PostMedia, StyleProfile
from runway.db.repositories import get_channel
from runway.discovery.providers import EnsembleSearchProvider
from runway.discovery.schemas import ImageSearchResult, SearchPage
from runway.evaluation.neural_challengers import NeuralChallengerEvaluator
from runway.intelligence.composed_retrieval import ComposedRetrievalService
from runway.intelligence.embeddings import ActiveRepresentationResolver, configuration_hash
from runway.intelligence.frontier_arena import (
    ArenaArmOutput,
    ArenaCasePlan,
    ArenaResponseImport,
    FrontierArenaService,
)
from runway.intelligence.profile import StyleProfileService
from runway.intelligence.representation_sets import RepresentationSetService
from runway.intelligence.topic_eligibility import TopicEligibilityService


def _finish_and_activate(
    service: RepresentationSetService,
    modality: str,
) -> int:
    planned = service.plan_history(modality)  # type: ignore[arg-type]
    representation_set_id = int(planned["representation_set_id"])
    for _ in range(20):
        result = service.backfill(representation_set_id, batch_size=4)
        if result["complete"] == result["expected"]:
            break
    else:
        raise AssertionError("fixture representation backfill did not complete")
    assert service.validate(representation_set_id)["valid"] is True
    service.activate(representation_set_id, reason=f"{modality} deterministic baseline")
    return representation_set_id


def _analysis(*, franchise: str, character: str, confidence: float) -> CandidateAnalysis:
    return CandidateAnalysis(
        franchise=franchise,
        characters=[character],
        scene_archetype="reaction",
        composition="close-up",
        emotion="surprised",
        text_overlay=False,
        watermark_probability=0.0,
        unsafe_probability=0.0,
        personal_artwork_probability=0.0,
        fan_art_probability=0.0,
        caption_potential=0.9,
        confidence=confidence,
        entities=[
            VisualEntity(
                name=character,
                entity_type="fictional_character",
                confidence=confidence,
                canonical_name=character,
            )
        ],
        objects=[],
        actions=["reacting"],
        relationships=[],
        setting="interior",
        ocr_text=[],
        field_confidence=CandidateFieldConfidence(
            entities=confidence,
            emotion=confidence,
            actions=confidence,
            scene=confidence,
            ocr=confidence,
        ),
    )


@pytest.mark.asyncio
async def test_active_representations_content_modes_and_semantic_topic_policy(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    await AnalysisService(database, settings).analyze_history()
    lifecycle = RepresentationSetService(database, settings)
    active_ids = {
        modality: _finish_and_activate(lifecycle, modality)
        for modality in ("text", "image", "multimodal")
    }
    profile = await StyleProfileService(database, settings).build()
    modes = profile["content_modes"]
    assert isinstance(modes, dict)
    assert modes["status"] == "learned"
    assert int(modes["mode_count"]) >= 2

    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    snapshot = ActiveRepresentationResolver(database).snapshot(channel_id)
    assert {
        modality: snapshot[modality]["representation_set_id"]  # type: ignore[index]
        for modality in active_ids
    } == active_ids
    assert all(
        snapshot[modality]["resolution"] == "active_set"  # type: ignore[index]
        for modality in active_ids
    )

    topic_service = TopicEligibilityService(database, settings)
    unfamiliar = _analysis(franchise="Futurama", character="Bender", confidence=0.55)
    exploratory = topic_service.evaluate(unfamiliar)
    assert exploratory.classification in {"adjacent", "exploratory"}
    assert exploratory.hard_reject is False
    assert exploratory.representation["representation_set_id"] == active_ids["text"]

    with database.session() as session:
        session.add(
            ChannelPolicyRule(
                channel_id=channel_id,
                rule_type="blocked_topic",
                scope="channel",
                priority=1000,
                value_json=json.dumps({"topics": ["futurama"]}),
                rule_text="Fixture explicit block",
                source="creator",
                version_hash=configuration_hash({"topics": ["futurama"]}),
                active=True,
            )
        )
    blocked = topic_service.evaluate(unfamiliar)
    assert blocked.classification == "blocked"
    assert blocked.hard_reject is True


@pytest.mark.asyncio
async def test_content_mode_backfill_creates_one_immutable_profile_revision(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    await AnalysisService(database, settings).analyze_history()
    lifecycle = RepresentationSetService(database, settings)
    _finish_and_activate(lifecycle, "multimodal")
    service = StyleProfileService(database, settings)
    await service.build()

    with database.session() as session:
        source = session.scalar(select(StyleProfile).where(StyleProfile.is_active.is_(True)))
        assert source is not None
        source_id = source.id
        legacy_profile = json.loads(source.profile_json)
        legacy_profile.pop("content_modes")
        legacy_profile.pop("explicit_channel_policy")
        source.profile_json = json.dumps(legacy_profile, sort_keys=True)

    created = service.backfill_content_modes()
    assert created["status"] == "created"
    assert created["source_profile_id"] == source_id
    modes = created["content_modes"]
    assert isinstance(modes, dict)
    assert modes["status"] == "learned"
    assert modes["mode_count"] >= 2

    with database.session() as session:
        old = session.get(StyleProfile, source_id)
        current = session.get(StyleProfile, int(created["profile_id"]))
        assert old is not None and old.is_active is False
        assert current is not None and current.is_active is True
        current_profile = json.loads(current.profile_json)
        assert current_profile["profile_derivation"] == {
            "kind": "content_mode_backfill",
            "source_profile_id": source_id,
            "source_profile_version": old.version,
            "content_mode_version": "channel-content-modes-v1",
            "generation_runtime_invoked": False,
        }

    unchanged = service.backfill_content_modes()
    assert unchanged["status"] == "unchanged"
    assert unchanged["profile_id"] == created["profile_id"]
    evaluation = service.evaluate()
    assert evaluation["evaluation_version"] == "current-intelligence-evaluation-v2"
    assert evaluation["current_pipeline"]["raw_frontier_arena"]["human_response_count"] == 0


def test_frontier_arena_is_three_arm_blind_and_fixture_labels_are_not_human(
    database: Database,
    settings: Settings,
    tmp_path: Path,
) -> None:
    CaptureService(database, settings).run_fixture()
    with database.session() as session:
        media_id = session.scalar(
            select(PostMedia.media_asset_id)
            .order_by(PostMedia.post_id, PostMedia.position)
            .limit(1)
        )
    assert media_id is not None
    service = FrontierArenaService(database, settings)
    shared = {
        "provider": "frontier-fixture",
        "model": "frontier-model",
        "prompt_version": "arena-v1",
        "cost_usd": 0.02,
        "latency_ms": 100.0,
    }
    planned = service.plan(
        study_key="fixture-three-arm-arena",
        cases=[
            ArenaCasePlan(
                case_key="case-1",
                media_asset_id=media_id,
                group_key="fixture-group",
                raw_frontier=ArenaArmOutput(caption="Raw caption", **shared),
                runway_frontier=ArenaArmOutput(caption="Runway caption", **shared),
                runway_weaker=ArenaArmOutput(
                    caption="Smaller Runway caption",
                    provider="local-fixture",
                    model="smaller-model",
                    prompt_version="arena-v1",
                    cost_usd=0.001,
                    latency_ms=30.0,
                ),
            )
        ],
        target_case_count=50,
    )
    study_id = int(planned["study_id"])
    export_path = service.export(study_id, tmp_path / "arena.json")
    exported = export_path.read_text(encoding="utf-8")
    assert "raw_frontier" not in exported
    assert "runway_frontier" not in exported
    assert "runway_weaker" not in exported
    imported = service.import_responses(
        study_id,
        responses=[ArenaResponseImport(case_key="case-1", choice="second")],
        review_session="engineering-regression",
        reviewer_kind="engineering_fixture",
    )
    assert imported["label_source"] == "engineering_fixture"
    status = service.status(study_id)
    report = service.report(study_id)
    assert status["human_response_count"] == 0
    assert report["human_responses"] == 0
    assert report["supports_runway_frontier_beats_raw"] is False
    assert report["supports_runway_weaker_beats_raw"] is False


def test_no_download_neural_readiness_and_composed_retrieval_boundary(
    database: Database,
    settings: Settings,
) -> None:
    readiness = NeuralChallengerEvaluator(settings).readiness()
    assert readiness["automatic_downloads"] is False
    assert readiness["implicit_activation"] is False
    challengers = readiness["challengers"]
    assert isinstance(challengers, list)
    assert any(row["modality"] == "text" for row in challengers)
    assert any(row["modality"] == "image_text" for row in challengers)
    assert all(row["status"] != "ready_for_offline_evaluation" for row in challengers)

    CaptureService(database, settings).run_fixture()
    with database.session() as session:
        media_ids = list(
            session.scalars(
                select(MediaAsset.id)
                .join(PostMedia, PostMedia.media_asset_id == MediaAsset.id)
                .order_by(MediaAsset.id)
                .limit(2)
            )
        )
    assert len(media_ids) == 2
    composed = ComposedRetrievalService(database, settings)
    created = composed.add_example(
        reference_media_asset_id=media_ids[0],
        modification_instruction="Keep the reaction, but use a group scene",
        target_media_asset_id=media_ids[1],
        label_source="policy",
        instruction_source={"fixture": True},
    )
    assert created["created"] is True
    composed_readiness = composed.readiness()
    assert composed_readiness["status"] == "blocked"
    assert composed_readiness["weighted_vector_fusion_is_learned"] is False
    assert composed_readiness["dataset"]["reviewed_human_examples"] == 0  # type: ignore[index]


class _StaticSearchProvider:
    def __init__(self, name: str, results: list[ImageSearchResult]):
        self.name = name
        self.results = results

    async def search(self, plan: SearchPlan, cursor: str | None = None) -> SearchPage:
        del plan, cursor
        return SearchPage(results=self.results)


@pytest.mark.asyncio
async def test_discovery_ensemble_merges_provenance_and_deduplicates() -> None:
    plan = SearchPlan(
        query_families=[SearchQueryFamily(purpose="reaction", queries=["reaction image"])],
        desired_visual_traits=["clear subject"],
        excluded_concepts=["nsfw"],
        desired_entities=[],
        desired_topics=["reaction"],
        desired_actions=[],
        desired_scenes=[],
        desired_compositions=[],
        source_policy="public_web_nsfw_blocked",
        rights_policy="provenance_only",
    )
    shared = ImageSearchResult(
        search_query="reaction image",
        result_rank=1,
        source_page_url="https://source.example/shared",
        direct_image_url="https://images.example/shared.jpg",
        source_domain="source.example",
    )
    unique = ImageSearchResult(
        search_query="reaction image",
        result_rank=2,
        source_page_url="https://second.example/unique",
        direct_image_url="https://images.example/unique.jpg",
        source_domain="second.example",
    )
    ensemble = EnsembleSearchProvider(
        [
            _StaticSearchProvider("first", [shared]),
            _StaticSearchProvider("second", [shared, unique]),
        ]
    )
    page = await ensemble.search(plan)
    assert len(page.results) == 2
    contributors = page.results[0].provider_metadata["ensemble_contributors"]
    assert contributors == ["first", "second"]
    assert ensemble.last_diagnostics["provider_contributions"] == {"first": 1, "second": 2}
