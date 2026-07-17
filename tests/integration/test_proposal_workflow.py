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
async def test_continuous_workflow_actions_and_restart_persistence(
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
    assert all(row["scheduled_publish_at"] is None for row in rows)
    assert all(row["caption_rationale"] for row in rows)
    assert all(row["caption_confidence"] is not None for row in rows)
    assert all(row["caption_reference_post_ids"] for row in rows)
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
    approved = proposals.approve(third_id)
    assert approved["scheduled_publish_at"] is not None
    scheduled = await publisher.schedule_post(third_id)
    assert scheduled.status == "internally_scheduled"
    assert (await publisher.verify_scheduled_post(third_id)).verified

    scheduled_slots = [datetime.fromisoformat(str(approved["scheduled_publish_at"]))]
    editable_approval_id = int(rows[3]["id"])
    initial_approval = proposals.approve(editable_approval_id)
    assert initial_approval["scheduled_publish_at"] is not None
    reset_approval = proposals.select_alternative(editable_approval_id, 0)
    assert reset_approval["status"] == "needs_review"
    assert reset_approval["scheduled_publish_at"] is None
    approved_again = proposals.approve(editable_approval_id)
    scheduled_slots.append(datetime.fromisoformat(str(approved_again["scheduled_publish_at"])))
    for row in rows[4:]:
        approved_row = proposals.approve(int(row["id"]))
        scheduled_slots.append(datetime.fromisoformat(str(approved_row["scheduled_publish_at"])))
    local_dates = [value.astimezone(toronto).date() for value in scheduled_slots]
    assert len(local_dates) == 8
    assert len(set(local_dates)) == 8
    assert all(value.astimezone(toronto).hour == 10 for value in scheduled_slots)

    database.engine.dispose()
    restarted = Database(settings)
    persisted = ProposalService(restarted, settings).detail(first_id)
    assert persisted["final_caption"] == "Human-edited final caption."
    assert persisted["status"] == "needs_review"
    restarted_proposals = ProposalService(restarted, settings)
    restarted_proposals.approve(first_id)
    restarted_proposals.approve(second_id)
    queue = restarted_proposals.queue_status()
    assert queue["coverage"] == 10
    assert len(queue["scheduled"]) == 10
    assert queue["posts_per_day"] == 1
    all_dates = [
        datetime.fromisoformat(str(row["scheduled_publish_at"])).astimezone(toronto).date()
        for row in queue["scheduled"]
    ]
    eleventh = restarted_proposals.next_available_slot().astimezone(toronto).date()
    assert (eleventh - min(all_dates)).days == 10

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
