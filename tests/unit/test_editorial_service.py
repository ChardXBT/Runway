from __future__ import annotations

import asyncio
from typing import Any

import pytest

from runway.config import Settings
from runway.db.base import Database
from runway.editorial.service import EditorialService
from runway.proposals.service import NoDistinctCandidateError


@pytest.mark.asyncio
async def test_editorial_generation_is_deduplicated_and_reports_progress(
    database: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EditorialService(database, settings)
    started = asyncio.Event()
    finish = asyncio.Event()
    review_count = 0

    def count_reviews() -> int:
        return review_count

    async def generate(_count: int) -> list[int]:
        nonlocal review_count
        started.set()
        await finish.wait()
        review_count = 1
        return [77]

    def result(
        generated_ids: list[int],
        discovery_result: dict[str, object] | None,
        detail: str,
    ) -> dict[str, Any]:
        return {
            "detail": detail,
            "generated_proposal_ids": generated_ids,
            "discovery": discovery_result,
            "next_proposal": None,
            "workflow": {},
        }

    monkeypatch.setattr(service, "_review_count", count_reviews)
    monkeypatch.setattr(service, "_unused_candidate_count", lambda: 1)
    monkeypatch.setattr(service, "_generate", generate)
    monkeypatch.setattr(service, "_result", result)

    first = asyncio.create_task(service.ensure_options(target=1, live_discovery=False))
    await started.wait()

    duplicate = await service.ensure_options(target=1, live_discovery=False)

    assert duplicate["generation"]["running"] is True
    assert "already running" in str(duplicate["detail"])

    finish.set()
    completed = await first

    assert completed["generated_proposal_ids"] == [77]
    assert completed["generation"]["running"] is False
    assert completed["generation"]["completed_at"] is not None


@pytest.mark.asyncio
async def test_editorial_failure_preserves_a_safe_retry_status(
    database: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EditorialService(database, settings)

    async def fail_generation(_count: int) -> list[int]:
        raise RuntimeError("private sqlite implementation detail")

    monkeypatch.setattr(service, "_review_count", lambda: 0)
    monkeypatch.setattr(service, "_unused_candidate_count", lambda: 1)
    monkeypatch.setattr(service, "_generate", fail_generation)

    with pytest.raises(RuntimeError, match="private sqlite"):
        await service.ensure_options(target=1, live_discovery=False)

    status = service.generation_status()
    assert status["running"] is False
    assert status["completed_at"] is not None
    assert status["detail"] == (
        "Generation paused before a new option was ready. Your review decisions are "
        "saved; press Generate more to start another search."
    )
    assert "sqlite" not in str(status["detail"]).casefold()


@pytest.mark.asyncio
async def test_editorial_searches_when_existing_candidates_are_not_distinct(
    database: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EditorialService(
        database,
        settings.model_copy(update={"enable_browser_search": True}),
    )
    review_count = 0
    discovery_providers: list[str] = []
    generate_calls = 0

    class FakeDiscoveryService:
        def __init__(self, _database: Database, _settings: Settings) -> None:
            pass

        async def discover(
            self,
            *,
            days: int,
            provider_name: str,
            live: bool = False,
        ) -> dict[str, object]:
            assert days == 1
            assert live is False
            discovery_providers.append(provider_name)
            return {
                "provider": provider_name,
                "accepted": 1 if provider_name == "frinkiac" else 0,
            }

    async def generate(_count: int) -> list[int]:
        nonlocal generate_calls, review_count
        generate_calls += 1
        if generate_calls < 3:
            raise NoDistinctCandidateError("stale candidate pool")
        review_count = 1
        return [88]

    monkeypatch.setattr(service, "_review_count", lambda: review_count)
    monkeypatch.setattr(service, "_unused_candidate_count", lambda: 1)
    monkeypatch.setattr(service, "_frame_archive_providers", lambda: ["frinkiac"])
    monkeypatch.setattr(service, "_generate", generate)
    monkeypatch.setattr(
        service,
        "_result",
        lambda generated, discovery, detail: {
            "detail": detail,
            "generated_proposal_ids": generated,
            "discovery": discovery,
        },
    )
    monkeypatch.setattr(
        "runway.editorial.service.DiscoveryService",
        FakeDiscoveryService,
    )

    result = await service.ensure_options(target=1)

    assert result["generated_proposal_ids"] == [88]
    assert discovery_providers == ["archives", "frinkiac"]
    assert generate_calls == 3
    assert result["generation"]["running"] is False


@pytest.mark.asyncio
async def test_editorial_uses_fallback_when_browser_search_fails(
    database: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EditorialService(
        database,
        settings.model_copy(update={"enable_browser_search": True}),
    )
    review_count = 0
    candidate_available = False
    discovery_providers: list[str] = []

    class FakeDiscoveryService:
        def __init__(self, _database: Database, _settings: Settings) -> None:
            pass

        async def discover(
            self,
            *,
            days: int,
            provider_name: str,
            live: bool = False,
        ) -> dict[str, object]:
            nonlocal candidate_available
            assert days == 1
            assert live is False
            discovery_providers.append(provider_name)
            if provider_name == "archives":
                raise RuntimeError("search challenge")
            candidate_available = True
            return {"provider": provider_name, "accepted": 1}

    async def generate(_count: int) -> list[int]:
        nonlocal review_count
        review_count = 1
        return [99]

    monkeypatch.setattr(service, "_review_count", lambda: review_count)
    monkeypatch.setattr(
        service,
        "_unused_candidate_count",
        lambda: int(candidate_available),
    )
    monkeypatch.setattr(service, "_frame_archive_providers", lambda: ["frinkiac"])
    monkeypatch.setattr(service, "_generate", generate)
    monkeypatch.setattr(
        service,
        "_result",
        lambda generated, discovery, detail: {
            "detail": detail,
            "generated_proposal_ids": generated,
            "discovery": discovery,
        },
    )
    monkeypatch.setattr(
        "runway.editorial.service.DiscoveryService",
        FakeDiscoveryService,
    )

    result = await service.ensure_options(target=1)

    assert result["generated_proposal_ids"] == [99]
    assert discovery_providers == ["archives", "frinkiac"]
    attempts = result["discovery"]["attempts"]
    assert attempts[0]["status"] == "failed"
    assert attempts[1]["provider"] == "frinkiac"


@pytest.mark.asyncio
async def test_editorial_continues_after_one_fallback_provider_fails(
    database: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = EditorialService(
        database,
        settings.model_copy(update={"enable_browser_search": True}),
    )
    review_count = 0
    candidate_available = False
    discovery_providers: list[str] = []

    class FakeDiscoveryService:
        def __init__(self, _database: Database, _settings: Settings) -> None:
            pass

        async def discover(
            self,
            *,
            days: int,
            provider_name: str,
            live: bool = False,
        ) -> dict[str, object]:
            nonlocal candidate_available
            assert days == 1
            assert live is False
            discovery_providers.append(provider_name)
            if provider_name in {"archives", "frinkiac"}:
                raise RuntimeError(f"{provider_name} unavailable")
            candidate_available = True
            return {"provider": provider_name, "accepted": 1}

    async def generate(_count: int) -> list[int]:
        nonlocal review_count
        review_count = 1
        return [101]

    monkeypatch.setattr(service, "_review_count", lambda: review_count)
    monkeypatch.setattr(
        service,
        "_unused_candidate_count",
        lambda: int(candidate_available),
    )
    monkeypatch.setattr(
        service,
        "_frame_archive_providers",
        lambda: ["frinkiac", "family-guy-wiki", "morbotron"],
    )
    monkeypatch.setattr(service, "_generate", generate)
    monkeypatch.setattr(
        service,
        "_result",
        lambda generated, discovery, detail: {
            "detail": detail,
            "generated_proposal_ids": generated,
            "discovery": discovery,
        },
    )
    monkeypatch.setattr(
        "runway.editorial.service.DiscoveryService",
        FakeDiscoveryService,
    )

    result = await service.ensure_options(target=1)

    assert result["generated_proposal_ids"] == [101]
    assert discovery_providers == ["archives", "frinkiac", "family-guy-wiki"]
    attempts = result["discovery"]["attempts"]
    assert [attempt["status"] for attempt in attempts[:2]] == ["failed", "failed"]
    assert attempts[2]["provider"] == "family-guy-wiki"


def test_editorial_includes_every_creator_approved_archive() -> None:
    assert EditorialService._secondary_archive_providers(
        ["Family Guy", "Futurama"],
    ) == ["family-guy-wiki", "morbotron"]
