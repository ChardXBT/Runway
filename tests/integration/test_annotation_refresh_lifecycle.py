from __future__ import annotations

import pytest
from sqlalchemy import select

from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import AnnotationRefreshItem, Post
from runway.intelligence.annotation_refresh import AnnotationRefreshService


@pytest.mark.asyncio
async def test_annotation_refresh_is_planned_resumable_and_activates_only_when_valid(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    service = AnnotationRefreshService(database, settings)
    planned = service.plan()
    repeated = service.plan()
    assert repeated["annotation_refresh_run_id"] == planned["annotation_refresh_run_id"]
    assert repeated["created"] is False
    run_id = int(planned["annotation_refresh_run_id"])

    partial = await service.run_batch(run_id, batch_size=3)
    assert partial["complete"] == 3
    assert partial["pending"] == 6
    with pytest.raises(ValueError, match="complete validated"):
        service.activate(run_id, reason="must fail closed")

    for _ in range(3):
        status = await service.run_batch(run_id, batch_size=3)
        if status["complete"] == status["expected"]:
            break
    validated = service.validate(run_id)
    assert validated["valid"] is True
    assert validated["coverage"] == 1.0
    activated = service.activate(run_id, reason="fixture refresh validated")
    assert activated["active"] is True
    assert activated["status"] == "active"


@pytest.mark.asyncio
async def test_annotation_refresh_detects_stale_content_and_codex_guard(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    service = AnnotationRefreshService(database, settings)
    planned = service.plan()
    run_id = int(planned["annotation_refresh_run_id"])
    with database.session() as session:
        first = session.scalar(select(Post).order_by(Post.id).limit(1))
        assert first is not None
        first.caption = f"{first.caption or ''} changed"
    await service.run_batch(run_id, batch_size=1)
    with database.session() as session:
        stale = session.scalar(
            select(AnnotationRefreshItem).where(
                AnnotationRefreshItem.annotation_refresh_run_id == run_id,
                AnnotationRefreshItem.post_id == first.id,
            )
        )
        assert stale is not None
        assert stale.status == "stale"

    codex_settings = settings.model_copy(update={"agent_runtime": "codex"})
    codex_service = AnnotationRefreshService(database, codex_settings)
    with pytest.raises(PermissionError, match="allow-model-calls"):
        await codex_service.run_batch(run_id, batch_size=1)
