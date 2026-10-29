from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy import func, select

from runway.captions.preferences import PairwiseCaptionPreferenceRanker
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import RepresentationRecord
from runway.db.repositories import get_channel
from runway.generation.providers import ImageGenerationProviderRegistry
from runway.intelligence import embeddings as embeddings_module
from runway.intelligence.embeddings import (
    ActiveRepresentationResolver,
    DeterministicImageEmbeddingProvider,
    DeterministicTextEmbeddingProvider,
    RepresentationProviderRegistry,
    RepresentationStore,
    bump_representation_set_epoch,
    content_hash,
)
from runway.intelligence.fusion import (
    EvidenceCandidate,
    mmr_select,
    reciprocal_rank_fusion,
)
from runway.intelligence.policies import ChannelPolicyService


def test_representations_are_idempotent_and_model_versions_coexist(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    provider = DeterministicTextEmbeddingProvider()
    result = provider.embed_text("focused person at a desk", purpose="caption_semantics")
    store = RepresentationStore(database)
    identity = {
        "channel_id": channel_id,
        "entity_type": "post",
        "entity_id": 41,
        "field": "caption",
        "modality": "text",
        "source_content_hash": content_hash("focused person at a desk"),
    }

    first = store.persist(result=result, **identity)
    repeated = store.persist(result=result, **identity)
    upgraded = store.persist(
        result=replace(result, version="fixture-next"),
        **identity,
    )

    assert repeated.id == first.id
    assert upgraded.id != first.id
    assert store.vectors(first).shape == (1, result.dimensions)
    with database.session() as session:
        assert session.scalar(select(func.count(RepresentationRecord.id))) == 2


def test_tokenless_text_still_produces_a_valid_normalized_representation() -> None:
    provider = DeterministicTextEmbeddingProvider()
    first = provider.embed_text("🔥🔥", purpose="caption_semantics")
    second = provider.embed_text("🔥🔥", purpose="caption_semantics")
    vectors = first.as_array()

    assert first.version == "2"
    assert first == second
    assert vectors.shape == (1, provider.dimensions)
    assert float((vectors[0] ** 2).sum()) == pytest.approx(1.0)


def test_resolver_memoizes_resolution_and_text_vectors(
    database: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolver = ActiveRepresentationResolver(database, RepresentationProviderRegistry())
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id

    active_set_calls = 0
    real_active_set = resolver.store.active_set

    def counting_active_set(**kwargs: object) -> object:
        nonlocal active_set_calls
        active_set_calls += 1
        return real_active_set(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(resolver.store, "active_set", counting_active_set)

    embed_calls = 0
    provider = resolver.registry.text("runway-local")
    real_embed = provider.embed_text

    def counting_embed(text: str, *, purpose: str) -> object:
        nonlocal embed_calls
        embed_calls += 1
        return real_embed(text, purpose=purpose)

    monkeypatch.setattr(provider, "embed_text", counting_embed)

    resolver.resolve(channel_id, modality="text")
    resolver.resolve(channel_id, modality="text")
    assert active_set_calls == 1

    resolver.text_vector(channel_id, "repeat me", score_purpose="caption_semantics")
    resolver.text_vector(channel_id, "repeat me", score_purpose="caption_semantics")
    resolver.text_vector(channel_id, "repeat me", score_purpose="feedback_topic")
    assert embed_calls == 2

    bump_representation_set_epoch()
    resolver.resolve(channel_id, modality="text")
    assert active_set_calls == 2


def test_image_embeddings_cache_by_file_state_and_persisted_entity(
    database: Database,
    settings: Settings,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspect_calls = 0

    class FakeFeatures:
        embedding = np.array([1.0, 0.0], dtype=np.float32)

    def counting_inspect(_path: Path) -> FakeFeatures:
        nonlocal inspect_calls
        inspect_calls += 1
        return FakeFeatures()

    monkeypatch.setattr(embeddings_module, "inspect_image", counting_inspect)
    provider = DeterministicImageEmbeddingProvider()
    image = tmp_path / "frame.png"
    image.write_bytes(b"one")

    provider.embed_image(image, purpose="test")
    provider.embed_image(image, purpose="test")
    assert inspect_calls == 1

    image.write_bytes(b"larger replacement content")
    provider.embed_image(image, purpose="test")
    assert inspect_calls == 2

    resolver = ActiveRepresentationResolver(database, RepresentationProviderRegistry())
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    kwargs = {
        "entity_type": "media_asset",
        "entity_id": 77,
        "field": "image",
        "source_content_hash": "fake-sha",
        "path": image,
        "score_purpose": "candidate_slate_semantic_diversity",
    }
    first, _resolution = resolver.image_vector_for_entity(channel_id, **kwargs)  # type: ignore[arg-type]
    assert inspect_calls == 3

    # A second resolver on the same database serves the persisted record
    # without re-inspecting the file.
    other = ActiveRepresentationResolver(database, RepresentationProviderRegistry())
    second, _resolution = other.image_vector_for_entity(channel_id, **kwargs)  # type: ignore[arg-type]
    assert inspect_calls == 3
    assert np.array_equal(first, second)


def test_provider_registries_never_fall_back_to_unrequested_paid_services() -> None:
    representations = RepresentationProviderRegistry()
    images = ImageGenerationProviderRegistry()

    assert representations.status() == {
        "text": ["runway-local"],
        "image": ["runway-local"],
        "multimodal": ["runway-local"],
        "multi_vector": ["runway-local"],
    }
    assert all(not row["paid_usage"] for row in images.status())
    with pytest.raises(LookupError, match="will not fall back"):
        representations.text("missing-provider")
    with pytest.raises(LookupError, match="will not fall back"):
        images.get("paid-provider")


def _evidence_pools() -> dict[str, list[EvidenceCandidate]]:
    return {
        "visual": [
            EvidenceCandidate(
                "post",
                1,
                "visual",
                0.99,
                duplicate_cluster="same-frame",
                evidence_role="visual_analogue",
            ),
            EvidenceCandidate(
                "post",
                2,
                "visual",
                0.98,
                duplicate_cluster="same-frame",
                evidence_role="visual_analogue",
            ),
            EvidenceCandidate(
                "post",
                3,
                "visual",
                0.80,
                duplicate_cluster="different-frame",
                evidence_role="visual_analogue",
            ),
        ],
        "semantic": [
            EvidenceCandidate(
                "post",
                1,
                "semantic",
                0.95,
                duplicate_cluster="same-frame",
                evidence_role="semantic_analogue",
            ),
            EvidenceCandidate(
                "post",
                3,
                "semantic",
                0.90,
                duplicate_cluster="different-frame",
                evidence_role="semantic_analogue",
            ),
            EvidenceCandidate(
                "post",
                2,
                "semantic",
                0.10,
                duplicate_cluster="same-frame",
                evidence_role="semantic_analogue",
            ),
        ],
    }


def test_fusion_is_deterministic_and_mmr_suppresses_duplicate_evidence() -> None:
    first = reciprocal_rank_fusion(_evidence_pools(), rank_constant=40)
    second = reciprocal_rank_fusion(_evidence_pools(), rank_constant=40)
    assert [(row.key, round(row.fusion_score, 12)) for row in first] == [
        (row.key, round(row.fusion_score, 12)) for row in second
    ]

    selected = mmr_select(
        first,
        limit=2,
        similarity=lambda left, right: (
            1.0 if left.duplicate_cluster == right.duplicate_cluster else 0.0
        ),
        lambda_relevance=0.72,
    )
    assert [row.entity_id for row in selected] == [1, 3]
    duplicate = next(row for row in first if row.entity_id == 2)
    assert duplicate.diversity_penalty > 0


def test_explicit_policy_outranks_defaults_and_rights_are_provenance_only(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    service = ChannelPolicyService(database, settings)
    defaults = service.ensure_defaults(channel_id)
    assert defaults.question_first is True

    service.add_rule(
        channel_id=channel_id,
        rule_type="caption_structure",
        priority=1000,
        value={
            "preferred": ["observation", "reaction", "open_question"],
            "question_first": False,
        },
        source="creator",
        rule_text="Lead with direct observations for this channel.",
    )
    current = service.current(channel_id)
    assert current.question_first is False
    assert current.preferred_structures[0] == "observation"

    assert (
        service.rights_decision(
            channel_id=channel_id,
            rights_status="unknown",
            for_generation=False,
        ).outcome
        == "allowed"
    )
    assert (
        service.rights_decision(
            channel_id=channel_id,
            rights_status="unknown",
            for_generation=True,
        ).outcome
        == "allowed"
    )
    assert (
        service.rights_decision(
            channel_id=channel_id,
            rights_status="blocked",
            explicitly_approved=True,
            for_generation=True,
        ).outcome
        == "allowed"
    )


def test_question_template_has_no_hardcoded_fallback_rank_bonus() -> None:
    components = {
        "structure_fit": 0.7,
        "grounding": 0.9,
        "policy": 0.8,
        "style": 0.6,
        "novelty": 0.7,
        "rotation": 0.7,
        "positive_feedback": 0.0,
        "negative_feedback_risk": 0.0,
        "pairing": 0.8,
    }
    question = PairwiseCaptionPreferenceRanker._fallback(
        "Why is the subject smiling?",
        components,
    )
    observation = PairwiseCaptionPreferenceRanker._fallback(
        "The subject is smiling.",
        components,
    )
    assert question == observation
