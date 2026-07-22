from __future__ import annotations

import pytest
from sqlalchemy import select

from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    Channel,
    Post,
    RepresentationRecord,
    RepresentationSet,
    RepresentationSetItem,
)
from runway.db.repositories import get_channel
from runway.intelligence.embeddings import (
    ActiveRepresentationResolver,
    DeterministicTextEmbeddingProvider,
    RepresentationProviderRegistry,
    RepresentationStore,
    content_hash,
)
from runway.intelligence.representation_sets import (
    REQUIRED_CHALLENGER_ACTIVATION_GATES,
    RepresentationSetService,
)


class FixtureTextProvider(DeterministicTextEmbeddingProvider):
    name = "fixture-v2"
    model = "signed-subword-concepts-fixture"
    version = "2"

    def __init__(self) -> None:
        super().__init__(dimensions=128)


def _finish_backfill(
    service: RepresentationSetService,
    representation_set_id: int,
) -> None:
    for _ in range(20):
        result = service.backfill(representation_set_id, batch_size=2)
        if result["complete"] == result["expected"]:
            return
    raise AssertionError("resumable representation backfill did not finish")


def test_representation_set_plan_resume_validate_activate_and_rollback(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    registry = RepresentationProviderRegistry()
    registry.register_text("fixture-v2", FixtureTextProvider())
    service = RepresentationSetService(database, settings, registry)

    first = service.plan_history("text")
    repeated = service.plan_history("text")
    assert repeated["representation_set_id"] == first["representation_set_id"]
    assert repeated["created"] is False

    first_id = int(first["representation_set_id"])
    partial = service.backfill(first_id, batch_size=2)
    assert partial["batch_completed"] == 2
    with pytest.raises(ValueError, match="complete, validated"):
        service.activate(first_id, reason="must fail closed")

    _finish_backfill(service, first_id)
    validated = service.validate(first_id)
    assert validated["valid"] is True
    assert validated["coverage"] == 1.0
    assert service.activate(first_id, reason="fixture baseline")["active"] is True

    second = service.plan_history("text", provider_name="fixture-v2")
    second_id = int(second["representation_set_id"])
    _finish_backfill(service, second_id)
    assert service.validate(second_id)["valid"] is True
    with pytest.raises(ValueError, match="requires passing evaluation gates"):
        service.activate(
            second_id,
            reason="incomplete fixture challenger evaluation",
            gate_results={"quality_non_regression": True},
        )
    activated = service.activate(
        second_id,
        reason="fixture challenger",
        gate_results={gate: True for gate in REQUIRED_CHALLENGER_ACTIVATION_GATES},
    )
    assert activated["active"] is True
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    resolved_provider, resolution = ActiveRepresentationResolver(database, registry).resolve(
        channel_id,
        modality="text",
    )
    assert resolved_provider.name == "fixture-v2"
    assert resolution.representation_set_id == second_id
    assert resolution.resolution == "active_set"
    with pytest.raises(LookupError, match="not fall back"):
        ActiveRepresentationResolver(database).resolve(
            channel_id,
            modality="text",
        )
    with database.session() as session:
        assert session.get(RepresentationSet, first_id).status == "superseded"  # type: ignore[union-attr]
        assert session.get(RepresentationSet, second_id).active is True  # type: ignore[union-attr]

    restored = service.rollback(second_id, reason="fixture rollback")
    assert restored["representation_set_id"] == first_id
    assert restored["active"] is True


def test_representation_store_exact_cache_active_resolution_and_stale_marking(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    service = RepresentationSetService(database, settings)
    planned = service.plan_history("text")
    set_id = int(planned["representation_set_id"])
    _finish_backfill(service, set_id)
    assert service.validate(set_id)["valid"] is True
    service.activate(set_id, reason="cache fixture")

    store = RepresentationStore(database)
    with database.session() as session:
        channel = get_channel(session, settings.channel_handle)
        post = session.scalar(select(Post).order_by(Post.id).limit(1))
        assert post is not None
        channel_id = channel.id
        post_id = post.id
        source_hash = content_hash(post.caption or "")
    active = store.get_active(
        channel_id=channel_id,
        scope="historical_text",
        purpose="historical_caption_semantics",
        entity_type="post",
        entity_id=post_id,
        field="caption",
        source_content_hash=source_hash,
    )
    assert active is not None
    assert store.diagnostics()["active_hits"] == 1

    calls = 0
    provider = DeterministicTextEmbeddingProvider()

    def produce() -> object:
        nonlocal calls
        calls += 1
        return provider.embed_text("cache me", purpose="candidate_text")

    first = store.get_or_create(
        channel_id=channel_id,
        entity_type="fixture",
        entity_id=1,
        field="text",
        modality="text",
        purpose="candidate_text",
        provider=provider.name,
        model=provider.model,
        model_version=provider.version,
        source_content_hash=content_hash("cache me"),
        configuration_hash=provider.configuration_fingerprint,
        producer=produce,  # type: ignore[arg-type]
    )
    second = store.get_or_create(
        channel_id=channel_id,
        entity_type="fixture",
        entity_id=1,
        field="text",
        modality="text",
        purpose="candidate_text",
        provider=provider.name,
        model=provider.model,
        model_version=provider.version,
        source_content_hash=content_hash("cache me"),
        configuration_hash=provider.configuration_fingerprint,
        producer=produce,  # type: ignore[arg-type]
    )
    assert first.id == second.id
    assert calls == 1

    assert (
        store.mark_stale(
            channel_id=channel_id,
            entity_type="post",
            entity_id=post_id,
            field="caption",
            current_source_content_hash=content_hash("changed"),
        )
        == 1
    )
    assert (
        store.active_set(
            channel_id=channel_id,
            scope="historical_text",
            purpose="historical_caption_semantics",
        )
        is None
    )
    with database.session() as session:
        item = session.scalar(
            select(RepresentationSetItem).where(
                RepresentationSetItem.representation_set_id == set_id,
                RepresentationSetItem.entity_id == post_id,
            )
        )
        assert item is not None
        assert item.status == "stale"


def test_invalid_vectors_cannot_validate_or_activate(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    service = RepresentationSetService(database, settings)
    planned = service.plan_history("text")
    set_id = int(planned["representation_set_id"])
    _finish_backfill(service, set_id)
    with database.session() as session:
        item = session.scalar(
            select(RepresentationSetItem)
            .where(RepresentationSetItem.representation_set_id == set_id)
            .order_by(RepresentationSetItem.id)
            .limit(1)
        )
        assert item is not None
        record = session.get(
            RepresentationRecord,
            item.representation_record_id,
        )
        assert record is not None
        record.serialized_data = b"\x00" * (record.dimensions * 4)

    validation = service.validate(set_id)
    assert validation["valid"] is False
    assert validation["errors"]
    with pytest.raises(ValueError, match="complete, validated"):
        service.activate(set_id, reason="invalid vectors must fail closed")


def test_active_representation_resolution_is_channel_isolated(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    service = RepresentationSetService(database, settings)
    planned = service.plan_history("text")
    set_id = int(planned["representation_set_id"])
    _finish_backfill(service, set_id)
    assert service.validate(set_id)["valid"] is True
    service.activate(set_id, reason="channel isolation fixture")
    with database.session() as session:
        post = session.scalar(select(Post).order_by(Post.id).limit(1))
        assert post is not None
        other = Channel(
            name="Other",
            handle="other-channel",
            timezone="UTC",
            default_post_time="10:00",
            planning_horizon_days=0,
            duplicate_window_days=30,
        )
        session.add(other)
        session.flush()
        other_channel_id = other.id

    store = RepresentationStore(database)
    assert (
        store.get_active(
            channel_id=other_channel_id,
            scope="historical_text",
            purpose="historical_caption_semantics",
            entity_type="post",
            entity_id=post.id,
            field="caption",
            source_content_hash=content_hash(post.caption or ""),
        )
        is None
    )
    assert store.diagnostics()["active_hits"] == 0
