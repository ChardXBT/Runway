from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from leeway.analysis.service import AnalysisService
from leeway.capture.service import CaptureService
from leeway.config import Settings
from leeway.db.base import Database
from leeway.db.models import AuditEvent, ProposalEvent
from leeway.discovery.service import DiscoveryService
from leeway.intelligence.profile import StyleProfileService
from leeway.proposals.service import ProposalService
from leeway.publishing.internal import InternalPublisher


@pytest.mark.asyncio
async def test_ten_day_workflow_actions_and_restart_persistence(
    database: Database, settings: Settings
) -> None:
    CaptureService(database, settings).run_fixture()
    await AnalysisService(database, settings).analyze_history()
    await StyleProfileService(database, settings).build()
    await DiscoveryService(database, settings).discover(provider_name="fixture", dry_run=True)

    proposals = ProposalService(database, settings)
    generated = await proposals.generate_batch(days=10, start_date=date(2026, 3, 5))
    assert len(generated["proposal_ids"]) == 10
    rows = proposals.list_proposals()
    assert len(rows) == 10
    timestamps = [datetime.fromisoformat(str(row["planned_publish_at"])) for row in rows]
    toronto = ZoneInfo("America/Toronto")
    assert [value.astimezone(toronto).hour for value in timestamps] == [10] * 10
    assert {value.utcoffset().total_seconds() for value in timestamps} == {-18000.0, -14400.0}

    first_id, second_id, third_id = (int(row["id"]) for row in rows[:3])
    proposals.edit_caption(first_id, "Human-edited final caption.")
    regenerated = await proposals.regenerate_captions(first_id)
    assert regenerated["final_caption"] == "Human-edited final caption."

    rejected = proposals.reject(second_id, "too repetitive")
    old_candidate = rejected["candidate_image_id"]
    replaced = await proposals.replace_image(second_id)
    assert replaced["status"] == "needs_review"
    assert replaced["candidate_image_id"] != old_candidate

    publisher = InternalPublisher(database, settings)
    with pytest.raises(ValueError, match="approved"):
        await publisher.schedule_post(third_id)
    proposals.approve(third_id)
    scheduled = await publisher.schedule_post(third_id)
    assert scheduled.status == "internally_scheduled"
    assert (await publisher.verify_scheduled_post(third_id)).verified

    database.engine.dispose()
    restarted = Database(settings)
    persisted = ProposalService(restarted, settings).detail(first_id)
    assert persisted["final_caption"] == "Human-edited final caption."
    assert persisted["status"] == "needs_review"
    queue = ProposalService(restarted, settings).queue_status(days=10, start_date=date(2026, 3, 5))
    assert queue["coverage"] == 10
    assert queue["conflicts"] == []

    with restarted.session() as session:
        event_types = set(session.scalars(select(ProposalEvent.event_type)).all())
        audit_types = set(session.scalars(select(AuditEvent.event_type)).all())
    assert {"caption_edited", "rejection_feedback", "image_replaced"} <= event_types
    assert {
        "proposal_rejected",
        "proposal_approved",
        "proposal_internally_scheduled",
    } <= audit_types
    restarted.engine.dispose()
