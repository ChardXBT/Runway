from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import func, select

from runway.captions.preferences import PairwiseCaptionPreferenceRanker
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import RepresentationRecord
from runway.db.repositories import get_channel
from runway.generation.providers import ImageGenerationProviderRegistry
from runway.intelligence.embeddings import (
    DeterministicTextEmbeddingProvider,
    RepresentationProviderRegistry,
    RepresentationStore,
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
        assert (
            session.scalar(select(func.count(RepresentationRecord.id)))
            == 2
        )


def test_tokenless_text_still_produces_a_valid_normalized_representation() -> None:
    provider = DeterministicTextEmbeddingProvider()
    first = provider.embed_text("🔥🔥", purpose="caption_semantics")
    second = provider.embed_text("🔥🔥", purpose="caption_semantics")
    vectors = first.as_array()

    assert first.version == "2"
    assert first == second
    assert vectors.shape == (1, provider.dimensions)
    assert float((vectors[0] ** 2).sum()) == pytest.approx(1.0)


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
    assert [
        (row.key, round(row.fusion_score, 12)) for row in first
    ] == [
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


def test_explicit_policy_outranks_defaults_and_unknown_rights_are_blocked(
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

    assert service.rights_decision(
        channel_id=channel_id,
        rights_status="unknown",
        for_generation=False,
    ).outcome == "requires_review"
    assert service.rights_decision(
        channel_id=channel_id,
        rights_status="unknown",
        for_generation=True,
    ).outcome == "blocked"
    assert service.rights_decision(
        channel_id=channel_id,
        rights_status="blocked",
        explicitly_approved=True,
        for_generation=True,
    ).outcome == "blocked"


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
