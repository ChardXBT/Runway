import json
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from runway.analysis.service import AnalysisService
from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    AuditEvent,
    CandidateImage,
    CaptionExposure,
    GenerationRun,
    ProposalEvent,
)
from runway.db.repositories import get_channel
from runway.discovery.service import DiscoveryService
from runway.intelligence.profile import StyleProfileService
from runway.proposals.service import ProposalService
from runway.publishing.internal import InternalPublisher
from runway.services.settings import SettingsService


def test_primary_franchise_supports_current_and_legacy_profile_keys() -> None:
    assert (
        ProposalService._primary_franchise(
            json.dumps({"topic_distribution": [["The Simpsons", 200]]})
        )
        == "The Simpsons"
    )
    assert (
        ProposalService._primary_franchise(
            json.dumps({"franchise_distribution": [["Futurama", 20]]})
        )
        == "Futurama"
    )


def test_proposal_service_recovers_only_abandoned_generation_runs(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
        stale = GenerationRun(
            channel_id=channel_id,
            style_profile_id=None,
            start_date=date(2026, 1, 1),
            days=1,
            status="running",
            started_at=datetime.now(UTC) - timedelta(hours=7),
        )
        active = GenerationRun(
            channel_id=channel_id,
            style_profile_id=None,
            start_date=date(2026, 1, 2),
            days=1,
            status="running",
            started_at=datetime.now(UTC),
        )
        long_batch = GenerationRun(
            channel_id=channel_id,
            style_profile_id=None,
            start_date=date(2026, 1, 3),
            days=500,
            status="running",
            started_at=datetime.now(UTC) - timedelta(hours=7),
        )
        session.add_all([stale, active, long_batch])
        session.flush()
        stale_id = stale.id
        active_id = active.id
        long_batch_id = long_batch.id

    service = ProposalService(database, settings)
    assert service._recover_stale_generation_runs() == 1

    with database.session() as session:
        stale = session.get(GenerationRun, stale_id)
        active = session.get(GenerationRun, active_id)
        long_batch = session.get(GenerationRun, long_batch_id)
        assert stale is not None
        assert stale.status == "failed"
        assert stale.completed_at is not None
        assert "Recovered abandoned" in str(stale.error_summary)
        assert active is not None
        assert active.status == "running"
        assert active.completed_at is None
        assert long_batch is not None
        assert long_batch.status == "running"
        assert long_batch.completed_at is None


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
    first_candidate_id = int(rows[0]["candidate_image_id"])
    with database.session() as session:
        first_candidate = session.get(CandidateImage, first_candidate_id)
        assert first_candidate is not None
        first_candidate.preview_asset_id = None
    first_without_preview = proposals.detail(int(rows[0]["id"]))
    assert (
        first_without_preview["candidate"]["preview_url"]
        == first_without_preview["candidate"]["original_url"]
    )
    assert all(row["scheduled_publish_at"] is None for row in rows)
    assert all(row["caption_rationale"] for row in rows)
    assert all(row["caption_confidence"] is not None for row in rows)
    assert all(row["caption_reference_post_ids"] for row in rows)
    timestamps = [datetime.fromisoformat(str(row["planned_publish_at"])) for row in rows]
    toronto = ZoneInfo("America/Toronto")
    assert [value.astimezone(toronto).hour for value in timestamps] == [10] * 10
    assert {value.utcoffset().total_seconds() for value in timestamps} == {-18000.0, -14400.0}

    first_id, second_id, third_id = (int(row["id"]) for row in rows[:3])
    proposals.edit_caption(first_id, "Human-edited final caption?!")
    regenerated = await proposals.regenerate_captions(first_id)
    assert regenerated["final_caption"] == "Human-edited final caption?!"

    with pytest.raises(ValueError, match="unsupported feedback reasons"):
        proposals.reject(
            second_id,
            "invalid QA reason",
            reason_codes=["not_in_the_taxonomy"],
            image_verdict="good",
        )
    assert proposals.detail(second_id)["status"] == "needs_review"

    rejected = proposals.reject(
        second_id,
        "too repetitive",
        reason_codes=["too_similar"],
        image_verdict="bad",
    )
    assert rejected["caption_feedback"][-1]["reason_codes"] == ["too_similar"]
    assert rejected["caption_feedback"][-1]["image_verdict"] == "bad"
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
    assert persisted["final_caption"] == "Human-edited final caption?!"
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


async def _generated_proposal_service(
    database: Database,
    settings: Settings,
    *,
    days: int,
) -> tuple[ProposalService, list[dict[str, object]]]:
    CaptureService(database, settings).run_fixture()
    await AnalysisService(database, settings).analyze_history()
    await StyleProfileService(database, settings).build()
    await DiscoveryService(database, settings).discover(provider_name="fixture", dry_run=True)
    proposals = ProposalService(database, settings)
    local_today = datetime.now(ZoneInfo(settings.timezone)).date()
    await proposals.generate_batch(days=days, start_date=local_today + timedelta(days=2))
    return proposals, proposals.list_proposals()


@pytest.mark.asyncio
async def test_atomic_editorial_accept_survives_secondary_learning_failure(
    database: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposals, rows = await _generated_proposal_service(database, settings, days=1)
    proposal_id = int(rows[0]["id"])
    exposure_decisions: list[str] = []
    candidate_events: list[str] = []

    def fail_feedback(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("secondary feedback unavailable")

    def record_exposure(*_args: object, **kwargs: object) -> None:
        exposure_decisions.append(str(kwargs["decision_type"]))

    def record_candidate(*_args: object, **kwargs: object) -> bool:
        candidate_events.append(str(kwargs["event_type"]))
        return True

    monkeypatch.setattr(proposals.feedback, "record", fail_feedback)
    monkeypatch.setattr(proposals.exposures, "record_decision", record_exposure)
    monkeypatch.setattr(
        proposals.candidate_exposures,
        "record_proposal_event",
        record_candidate,
    )
    accepted = proposals.accept_to_lineup(proposal_id, "Human final caption?!")

    assert accepted["status"] == "internally_scheduled"
    assert accepted["final_caption"] == "Human final caption?!"
    assert accepted["scheduled_publish_at"] is not None
    assert ProposalService(database, settings).detail(proposal_id)["status"] == (
        "internally_scheduled"
    )
    assert exposure_decisions == ["edited", "accepted"]
    assert candidate_events == ["accepted"]


@pytest.mark.asyncio
async def test_proposal_history_can_request_newest_records_first(
    database: Database,
    settings: Settings,
) -> None:
    proposals, rows = await _generated_proposal_service(database, settings, days=2)

    ascending = [int(row["id"]) for row in rows]
    descending = [int(row["id"]) for row in proposals.list_proposals(order="desc")]

    assert descending == list(reversed(ascending))
    requested_id = ascending[-1]
    requested = proposals.next_for_review(proposal_id=requested_id)
    assert requested is not None
    assert requested["id"] == requested_id
    with database.session() as session:
        exposed_proposal_ids = session.scalars(
            select(CaptionExposure.proposal_id).order_by(CaptionExposure.proposal_id)
        ).all()
    assert exposed_proposal_ids == [requested_id]


@pytest.mark.asyncio
async def test_secondary_display_evidence_cannot_hide_a_review_option(
    database: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposals, rows = await _generated_proposal_service(database, settings, days=1)

    def fail_display(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("display evidence unavailable")

    candidate_events: list[str] = []

    def record_candidate(*_args: object, **kwargs: object) -> bool:
        candidate_events.append(str(kwargs["event_type"]))
        return True

    monkeypatch.setattr(proposals.exposures, "record_display", fail_display)
    monkeypatch.setattr(
        proposals.candidate_exposures,
        "record_proposal_event",
        record_candidate,
    )
    result = proposals.next_for_review()

    assert result is not None
    assert result["id"] == rows[0]["id"]
    assert result["status"] == "needs_review"
    assert candidate_events == ["shown"]


@pytest.mark.asyncio
async def test_timezone_change_is_blocked_while_lineup_work_is_active(
    database: Database,
    settings: Settings,
) -> None:
    proposals, rows = await _generated_proposal_service(database, settings, days=1)
    proposals.accept_to_lineup(int(rows[0]["id"]), str(rows[0]["final_caption"]))

    with pytest.raises(ValueError, match="timezone cannot change"):
        SettingsService(database, settings).update({"timezone": "America/Vancouver"})


def test_channel_handle_is_not_a_general_editable_setting(
    database: Database,
    settings: Settings,
) -> None:
    with pytest.raises(ValueError, match="unsupported settings: handle"):
        SettingsService(database, settings).update({"handle": "AnotherChannel"})
