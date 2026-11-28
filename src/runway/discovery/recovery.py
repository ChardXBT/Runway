from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult

from runway.config import Settings
from runway.db.base import Database
from runway.db.job_leases import complete_stale_run, run_lease_expired
from runway.db.models import ImageGenerationRun, SearchRun
from runway.db.repositories import audit, get_channel
from runway.domain.enums import RunStatus


def _image_generation_run_id(search_run: SearchRun) -> int | None:
    if not search_run.provider.startswith("generated:"):
        return None
    try:
        payload = json.loads(search_run.query_plan_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("source") != "image_generation":
        return None
    value = payload.get("generation_run_id")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


def recover_stale_search_runs(database: Database, settings: Settings) -> int:
    """Close abandoned search workspace rows and their generated-image owners."""

    now = datetime.now(UTC)
    fallback_after = timedelta(hours=6)
    recovered = 0
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
        rows = session.scalars(
            select(SearchRun).where(
                SearchRun.channel_id == channel_id,
                SearchRun.status == RunStatus.RUNNING.value,
            )
        ).all()
        for row in rows:
            if not run_lease_expired(row, now, fallback_after=fallback_after):
                continue
            generation_run_id = _image_generation_run_id(row)
            if not complete_stale_run(
                session,
                SearchRun,
                row.id,
                owner_token=row.owner_token,
                lease_version=row.lease_version,
                values={
                    "status": RunStatus.FAILED.value,
                    "completed_at": now,
                    "error_summary": "Recovered abandoned discovery run without a valid lease.",
                },
            ):
                continue
            generated_run_closed = False
            if generation_run_id is not None:
                result = cast(
                    CursorResult[Any],
                    session.execute(
                        update(ImageGenerationRun)
                        .where(
                            ImageGenerationRun.id == generation_run_id,
                            ImageGenerationRun.status == RunStatus.RUNNING.value,
                            ImageGenerationRun.completed_at.is_(None),
                        )
                        .values(
                            status=RunStatus.FAILED.value,
                            completed_at=now,
                            error_summary=(
                                "Recovered abandoned image generation run without a valid lease."
                            ),
                        )
                    ),
                )
                generated_run_closed = result.rowcount == 1
            audit(
                session,
                "search_stale_run_recovered",
                "search_run",
                row.id,
                {
                    "started_at": row.started_at.isoformat(),
                    "owner_token_present": row.owner_token is not None,
                    "lease_version": row.lease_version,
                    "image_generation_run_id": generation_run_id,
                    "image_generation_run_closed": generated_run_closed,
                },
            )
            recovered += 1
    return recovered
