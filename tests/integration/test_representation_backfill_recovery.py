from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import RepresentationRecord, RepresentationSetItem
from runway.intelligence.embeddings import (
    DeterministicTextEmbeddingProvider,
    RepresentationProviderRegistry,
    RepresentationResult,
)
from runway.intelligence.representation_sets import RepresentationSetService


def _service(
    database: Database,
    settings: Settings,
    registry: RepresentationProviderRegistry | None = None,
) -> RepresentationSetService:
    return RepresentationSetService(database, settings, registry)


def _planned_text_set(
    database: Database,
    settings: Settings,
) -> tuple[RepresentationSetService, int, int]:
    CaptureService(database, settings).run_fixture()
    service = _service(database, settings)
    planned = service.plan_history("text")
    return service, int(planned["representation_set_id"]), int(planned["expected"])


def _items(database: Database, set_id: int) -> list[RepresentationSetItem]:
    with database.session() as session:
        return list(
            session.scalars(
                select(RepresentationSetItem)
                .where(RepresentationSetItem.representation_set_id == set_id)
                .order_by(RepresentationSetItem.id)
            ).all()
        )


def test_orphaned_running_backfill_items_are_recovered(
    database: Database,
    settings: Settings,
) -> None:
    service, set_id, expected = _planned_text_set(database, settings)
    with database.session() as session:
        items = list(
            session.scalars(
                select(RepresentationSetItem).where(
                    RepresentationSetItem.representation_set_id == set_id
                )
            ).all()
        )
        for item in items:
            item.status = "running"
            item.attempts = 1
            item.error_summary = None

    recovered = service.backfill(set_id, batch_size=10_000)

    assert recovered["batch_attempted"] == expected
    assert recovered["batch_completed"] == expected
    assert recovered["batch_failed"] == 0
    assert recovered["complete"] == expected
    assert recovered["status"] == "backfilled"
    assert service.validate(set_id)["valid"] is True


def test_crash_before_and_after_embedding_both_recover_without_duplicate_records(
    database: Database,
    settings: Settings,
) -> None:
    service, set_id, expected = _planned_text_set(database, settings)
    first = service.backfill(set_id, batch_size=1)
    assert first["batch_completed"] == 1
    completed_item = _items(database, set_id)[0]
    assert completed_item.representation_record_id is not None
    record_id = completed_item.representation_record_id

    with database.session() as session:
        record_count = int(session.scalar(select(func.count(RepresentationRecord.id))) or 0)
        item = session.get(RepresentationSetItem, completed_item.id)
        assert item is not None
        # Crash after the representation was embedded but before the item was linked.
        item.status = "running"
        item.representation_record_id = None
        item.completed_at = None
        item.claimed_at = datetime.now(UTC) - timedelta(seconds=301)

    after_embedding = service.backfill(set_id, batch_size=1)
    assert after_embedding["batch_completed"] == 1
    repaired = _items(database, set_id)[0]
    assert repaired.status == "complete"
    assert repaired.representation_record_id == record_id
    with database.session() as session:
        assert int(session.scalar(select(func.count(RepresentationRecord.id))) or 0) == record_count

    with database.session() as session:
        item = session.scalars(
            select(RepresentationSetItem)
            .where(
                RepresentationSetItem.representation_set_id == set_id,
                RepresentationSetItem.status == "pending",
            )
            .order_by(RepresentationSetItem.id)
            .limit(1)
        ).first()
        assert item is not None
        # Crash before any embedding work or representation row existed.
        item.status = "running"
        item.claimed_at = datetime.now(UTC) - timedelta(seconds=301)
        item_id = item.id

    before_embedding = service.backfill(set_id, batch_size=2)
    assert before_embedding["batch_failed"] == 0
    assert before_embedding["batch_completed"] >= 1
    assert any(
        item.id == item_id and item.status == "complete" for item in _items(database, set_id)
    )
    assert sum(item.status == "complete" for item in _items(database, set_id)) < expected


def test_stale_complete_failed_and_running_mixture_preserves_stale_gate(
    database: Database,
    settings: Settings,
) -> None:
    service, set_id, expected = _planned_text_set(database, settings)
    assert service.backfill(set_id, batch_size=1)["batch_completed"] == 1
    items = _items(database, set_id)
    assert expected >= 4
    stale_item = items[1]
    failed_item = items[2]
    running_item = items[3]
    with database.session() as session:
        stale = session.get(RepresentationSetItem, stale_item.id)
        failed = session.get(RepresentationSetItem, failed_item.id)
        running = session.get(RepresentationSetItem, running_item.id)
        assert stale is not None and failed is not None and running is not None
        stale.status = "stale"
        stale.error_summary = "source content hash changed"
        failed.status = "failed"
        failed.error_summary = "transient fixture failure"
        running.status = "running"
        running.claimed_at = datetime.now(UTC) - timedelta(seconds=301)

    recovered = service.backfill(set_id, batch_size=10_000)
    final_items = _items(database, set_id)
    by_id = {item.id: item for item in final_items}

    assert recovered["batch_failed"] == 0
    assert by_id[stale_item.id].status == "stale"
    assert by_id[failed_item.id].status == "complete"
    assert by_id[running_item.id].status == "complete"
    assert sum(item.status == "complete" for item in final_items) == expected - 1
    validation = service.validate(set_id)
    assert validation["valid"] is False
    assert any(f"item {stale_item.id} is stale" in error for error in validation["errors"])
    with pytest.raises(ValueError, match="complete, validated"):
        service.activate(set_id, reason="stale item must fail closed")


def test_duplicate_plan_item_identities_are_rejected_by_the_database_contract(
    database: Database,
    settings: Settings,
) -> None:
    service, set_id, expected = _planned_text_set(database, settings)
    original = _items(database, set_id)[0]

    with pytest.raises(IntegrityError), database.session() as session:
        session.add(
            RepresentationSetItem(
                representation_set_id=set_id,
                channel_id=original.channel_id,
                entity_type=original.entity_type,
                entity_id=original.entity_id,
                field=original.field,
                source_content_hash=original.source_content_hash,
                source_locator_json=original.source_locator_json,
                status="pending",
                attempts=0,
            )
        )
        session.flush()

    assert len(_items(database, set_id)) == expected
    assert service.backfill(set_id, batch_size=10_000)["batch_completed"] == expected
    assert service.validate(set_id)["valid"] is True


class CountingTextProvider(DeterministicTextEmbeddingProvider):
    name = "counting-fixture"
    model = "counting-signed-subword-concepts"
    version = "1"

    def __init__(self) -> None:
        super().__init__(dimensions=128)
        self.calls: list[str] = []
        self._lock = threading.Lock()

    def embed_text(self, text: str, *, purpose: str) -> RepresentationResult:
        with self._lock:
            self.calls.append(text)
        time.sleep(0.005)
        return super().embed_text(text, purpose=purpose)


def test_concurrent_backfill_workers_claim_each_item_once(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    provider = CountingTextProvider()
    registry = RepresentationProviderRegistry()
    registry.register_text(provider.name, provider)
    service = _service(database, settings, registry)
    planned = service.plan_history("text", provider_name=provider.name)
    set_id = int(planned["representation_set_id"])
    expected = int(planned["expected"])
    services = [_service(database, settings, registry) for _ in range(4)]

    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(
            executor.map(
                lambda worker: worker.backfill(set_id, batch_size=expected),
                services,
            )
        )

    final_items = _items(database, set_id)
    assert sum(int(result["batch_attempted"]) for result in results) == expected
    assert len(provider.calls) == expected
    assert len(set(provider.calls)) == expected
    assert all(item.status == "complete" for item in final_items)
    assert sum(item.attempts for item in final_items) == expected
    assert service.validate(set_id)["valid"] is True


class BlockingTextProvider(DeterministicTextEmbeddingProvider):
    name = "blocking-fixture"
    model = "blocking-signed-subword-concepts"
    version = "1"

    def __init__(self) -> None:
        super().__init__(dimensions=128)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def embed_text(self, text: str, *, purpose: str) -> RepresentationResult:
        self.calls += 1
        self.entered.set()
        if not self.release.wait(2):
            raise AssertionError("blocking fixture was not released")
        return super().embed_text(text, purpose=purpose)


def test_expired_claim_fencing_prevents_stale_worker_completion(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    provider = BlockingTextProvider()
    registry = RepresentationProviderRegistry()
    registry.register_text(provider.name, provider)
    service = _service(database, settings, registry)
    planned = service.plan_history("text", provider_name=provider.name)
    set_id = int(planned["representation_set_id"])
    target = _items(database, set_id)[0]
    initial_attempt = service._claim_item(target.id)
    assert initial_attempt == 1

    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_completion = executor.submit(
            service._backfill_item,
            target.id,
            claim_attempt=initial_attempt,
        )
        assert provider.entered.wait(2)
        with database.session() as session:
            item = session.get(RepresentationSetItem, target.id)
            assert item is not None
            item.claimed_at = datetime.now(UTC) - timedelta(seconds=301)
        reclaimed_attempt = service._claim_item(target.id)
        assert reclaimed_attempt == initial_attempt + 1
        provider.release.set()
        assert stale_completion.result(timeout=5) is False

    blocked = next(item for item in _items(database, set_id) if item.id == target.id)
    assert blocked.status == "running"
    assert blocked.attempts == reclaimed_attempt
    assert (
        service._fail_item_claim(
            target.id,
            initial_attempt,
            RuntimeError("stale worker finished too late"),
        )
        is False
    )
    assert service._backfill_item(target.id, claim_attempt=reclaimed_attempt) is True
    completed = next(item for item in _items(database, set_id) if item.id == target.id)
    assert completed.status == "complete"
    assert completed.attempts == reclaimed_attempt
    assert completed.representation_record_id is not None
    assert provider.calls == 1


def test_running_lease_is_conservative_under_clock_changes(
    database: Database,
    settings: Settings,
) -> None:
    service, set_id, _expected = _planned_text_set(database, settings)
    target = _items(database, set_id)[0]

    with database.session() as session:
        item = session.get(RepresentationSetItem, target.id)
        assert item is not None
        item.status = "running"
        item.attempts = 1
        item.claimed_at = datetime.now(UTC)
    service.backfill(set_id, batch_size=10_000)
    fresh = next(item for item in _items(database, set_id) if item.id == target.id)
    assert fresh.status == "running"
    assert fresh.attempts == 1

    with database.session() as session:
        item = session.get(RepresentationSetItem, target.id)
        assert item is not None
        item.claimed_at = datetime.now(UTC) + timedelta(hours=1)
    service.backfill(set_id, batch_size=10_000)
    future = next(item for item in _items(database, set_id) if item.id == target.id)
    assert future.status == "running"
    assert future.attempts == 1

    with database.session() as session:
        item = session.get(RepresentationSetItem, target.id)
        assert item is not None
        item.claimed_at = datetime.now(UTC) - timedelta(seconds=301)
    service.backfill(set_id, batch_size=10_000)
    expired = next(item for item in _items(database, set_id) if item.id == target.id)
    assert expired.status == "complete"
    assert expired.attempts == 2


def test_corrupt_source_hash_fails_closed_then_recovers_after_repair(
    database: Database,
    settings: Settings,
) -> None:
    service, set_id, expected = _planned_text_set(database, settings)
    assert expected > 1
    assert service.backfill(set_id, batch_size=expected - 1)["batch_completed"] == expected - 1
    target = next(item for item in _items(database, set_id) if item.status == "pending")
    original_hash = target.source_content_hash
    with database.session() as session:
        item = session.get(RepresentationSetItem, target.id)
        assert item is not None
        item.source_content_hash = "corrupt-source-hash"

    corrupted = service.backfill(set_id, batch_size=1)
    assert corrupted["batch_attempted"] == 1
    assert corrupted["batch_completed"] == 0
    assert corrupted["batch_failed"] == 1
    corrupt_item = next(item for item in _items(database, set_id) if item.id == target.id)
    assert corrupt_item.status == "failed"
    assert corrupt_item.error_summary is not None
    assert "source content changed" in corrupt_item.error_summary
    validation = service.validate(set_id)
    assert validation["valid"] is False
    with pytest.raises(ValueError, match="complete, validated"):
        service.activate(set_id, reason="corrupt source must fail closed")

    with database.session() as session:
        item = session.get(RepresentationSetItem, target.id)
        assert item is not None
        item.source_content_hash = original_hash
    repaired = service.backfill(set_id, batch_size=1)
    assert repaired["batch_completed"] == 1
    assert service.validate(set_id)["valid"] is True


def test_empty_plan_and_huge_or_empty_batches_remain_bounded(
    database: Database,
    settings: Settings,
) -> None:
    service = _service(database, settings)
    empty = service.plan_history("text")
    empty_id = int(empty["representation_set_id"])
    assert empty["expected"] == 0
    empty_batch = service.backfill(empty_id, batch_size=10_000)
    assert empty_batch["batch_attempted"] == 0
    assert empty_batch["batch_completed"] == 0
    assert empty_batch["batch_failed"] == 0
    assert service.validate(empty_id)["valid"] is True

    CaptureService(database, settings).run_fixture()
    planned = service.plan_history("text")
    set_id = int(planned["representation_set_id"])
    expected = int(planned["expected"])
    with pytest.raises(ValueError, match="batch_size"):
        service.backfill(set_id, batch_size=0)
    with pytest.raises(ValueError, match="batch_size"):
        service.backfill(set_id, batch_size=10_001)

    huge = service.backfill(set_id, batch_size=10_000)
    assert huge["batch_attempted"] == expected
    assert huge["batch_completed"] == expected
    drained = service.backfill(set_id, batch_size=10_000)
    assert drained["batch_attempted"] == 0
    assert drained["batch_completed"] == 0
    assert drained["batch_failed"] == 0
