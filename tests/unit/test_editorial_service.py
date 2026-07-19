from __future__ import annotations

import asyncio
from typing import Any

import pytest

from runway.config import Settings
from runway.db.base import Database
from runway.editorial.service import EditorialService


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
