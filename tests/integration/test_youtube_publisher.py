from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select

from runway.analysis.service import AnalysisService
from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import AuditEvent, CandidateImage, Proposal, PublishAttempt
from runway.db.repositories import get_channel
from runway.discovery.service import DiscoveryService
from runway.intelligence.profile import StyleProfileService
from runway.proposals.service import ProposalService
from runway.publishing.base import PreparedPost, PublisherSessionStatus
from runway.publishing.internal import InternalPublisher
from runway.publishing.queue import PublisherQueueCoordinator
from runway.publishing.youtube import (
    BrowserMutationReceipt,
    BrowserScheduleReceipt,
    YouTubeBrowserPublisher,
)


class FakeYouTubeAdapter:
    def __init__(self) -> None:
        self.validate_calls = 0
        self.session_valid = True
        self.session_detail = "Qlob Editor fixture session valid."
        self.schedule_calls: list[PreparedPost] = []
        self.verify_calls: list[PreparedPost] = []
        self.edit_calls: list[tuple[PreparedPost, PreparedPost]] = []
        self.remove_calls: list[PreparedPost] = []
        self.schedule_receipt = BrowserScheduleReceipt(
            submitted=True,
            verified=True,
            external_id="Ugfixture",
            external_url="https://www.youtube.com/post/Ugfixture",
            detail="Fixture scheduled post verified.",
        )
        self.verify_receipt = self.schedule_receipt
        self.edit_receipt = BrowserMutationReceipt(
            applied=True,
            verified=True,
            detail="Fixture scheduled post edit verified.",
        )
        self.remove_receipt = BrowserMutationReceipt(
            applied=True,
            verified=True,
            detail="Fixture scheduled post removal verified.",
        )

    async def validate_session(self) -> PublisherSessionStatus:
        self.validate_calls += 1
        return PublisherSessionStatus(
            valid=self.session_valid,
            publisher="fixture",
            detail=self.session_detail,
            checks={
                "configured channel URL": self.session_valid,
                "Community composer": self.session_valid,
            },
        )

    async def schedule(self, post: PreparedPost) -> BrowserScheduleReceipt:
        self.schedule_calls.append(post)
        return self.schedule_receipt

    async def verify(self, post: PreparedPost) -> BrowserScheduleReceipt:
        self.verify_calls.append(post)
        return self.verify_receipt

    async def edit(
        self,
        current: PreparedPost,
        updated: PreparedPost,
    ) -> BrowserMutationReceipt:
        self.edit_calls.append((current, updated))
        return self.edit_receipt

    async def remove(self, post: PreparedPost) -> BrowserMutationReceipt:
        self.remove_calls.append(post)
        return self.remove_receipt


async def _scheduled_proposal(database: Database, settings: Settings) -> int:
    CaptureService(database, settings).run_fixture()
    await AnalysisService(database, settings).analyze_history()
    await StyleProfileService(database, settings).build()
    await DiscoveryService(database, settings).discover(provider_name="fixture", dry_run=True)
    local_today = datetime.now(ZoneInfo(settings.timezone)).date()
    proposals = ProposalService(database, settings)
    generated = await proposals.generate_batch(
        days=1,
        start_date=local_today + timedelta(days=2),
    )
    proposal_id = int(generated["proposal_ids"][0])
    proposals.approve(proposal_id)
    result = await InternalPublisher(database, settings).schedule_post(proposal_id)
    assert result.status == "internally_scheduled"
    return proposal_id


async def _scheduled_proposal_pair(
    database: Database,
    settings: Settings,
) -> tuple[int, int]:
    CaptureService(database, settings).run_fixture()
    await AnalysisService(database, settings).analyze_history()
    await StyleProfileService(database, settings).build()
    await DiscoveryService(database, settings).discover(provider_name="fixture", dry_run=True)
    local_today = datetime.now(ZoneInfo(settings.timezone)).date()
    proposals = ProposalService(database, settings)
    generated = await proposals.generate_batch(
        days=2,
        start_date=local_today + timedelta(days=2),
    )
    proposal_ids = tuple(int(value) for value in generated["proposal_ids"])
    assert len(proposal_ids) == 2
    publisher = InternalPublisher(database, settings)
    for proposal_id in proposal_ids:
        proposals.approve(proposal_id)
        await publisher.schedule_post(proposal_id)
    return proposal_ids


@pytest.mark.asyncio
async def test_publisher_requires_feature_gate_and_exact_one_time_confirmation(
    database: Database,
    settings: Settings,
) -> None:
    proposal_id = await _scheduled_proposal(database, settings)
    adapter = FakeYouTubeAdapter()
    disabled = YouTubeBrowserPublisher(database, settings, adapter)
    with pytest.raises(ValueError, match="PUBLISHING_ENABLED=false"):
        await disabled.prepare_attempt(proposal_id)
    assert adapter.validate_calls == 0
    assert adapter.schedule_calls == []

    enabled_settings = settings.model_copy(update={"publishing_enabled": True})
    publisher = YouTubeBrowserPublisher(database, enabled_settings, adapter)
    preparation = await publisher.prepare_attempt(proposal_id)
    assert preparation.confirmation_phrase == f"SCHEDULE QLOB #{proposal_id}"
    assert adapter.schedule_calls == []

    with pytest.raises(ValueError, match="exact confirmation phrase"):
        await publisher.confirm_schedule(
            preparation.attempt_id,
            confirmation_token=preparation.confirmation_token,
            confirmation_phrase="schedule it",
        )
    assert adapter.schedule_calls == []
    assert publisher.attempt_status(preparation.attempt_id)["status"] == "prepared"

    with pytest.raises(ValueError, match="caption editing requires"):
        ProposalService(database, settings).edit_caption(proposal_id, "Changed too late.")

    result = await publisher.confirm_schedule(
        preparation.attempt_id,
        confirmation_token=preparation.confirmation_token,
        confirmation_phrase=preparation.confirmation_phrase,
    )
    assert result.status == "externally_scheduled"
    assert len(adapter.schedule_calls) == 1
    assert publisher.attempt_status(preparation.attempt_id)["status"] == "verified"
    with database.session() as session:
        attempt = session.get(PublishAttempt, preparation.attempt_id)
        assert attempt is not None
        assert attempt.confirmation_token_hash != preparation.confirmation_token
        proposal = session.get(Proposal, proposal_id)
        assert proposal is not None
        assert proposal.external_post_id == "Ugfixture"

    with pytest.raises(ValueError, match="verified, not prepared"):
        await publisher.confirm_schedule(
            preparation.attempt_id,
            confirmation_token=preparation.confirmation_token,
            confirmation_phrase=preparation.confirmation_phrase,
        )
    assert len(adapter.schedule_calls) == 1


@pytest.mark.asyncio
async def test_publisher_connection_status_is_durable_and_becomes_stale(
    database: Database,
    settings: Settings,
) -> None:
    adapter = FakeYouTubeAdapter()
    enabled_settings = settings.model_copy(update={"publishing_enabled": True})
    publisher = YouTubeBrowserPublisher(database, enabled_settings, adapter)

    assert publisher.connection_status()["state"] == "unchecked"
    checked = await publisher.validate_session()
    assert checked.valid is True
    connected = publisher.connection_status()
    assert connected["state"] == "connected"
    assert connected["checks"] == {
        "configured channel URL": True,
        "Community composer": True,
    }

    with database.session() as session:
        validation = session.scalar(
            select(AuditEvent)
            .where(AuditEvent.event_type == "youtube_session_validated")
            .order_by(AuditEvent.id.desc())
            .limit(1)
        )
        assert validation is not None
        validation.created_at = datetime.now(UTC) - timedelta(hours=25)
    stale = publisher.connection_status()
    assert stale["state"] == "stale"
    assert stale["stale"] is True

    adapter.session_valid = False
    adapter.session_detail = "Sign in to the Qlob Editor account."
    failed = await publisher.validate_session()
    assert failed.valid is False
    needs_attention = publisher.connection_status()
    assert needs_attention["state"] == "needs_attention"
    assert needs_attention["valid"] is False
    assert needs_attention["checked_at"] is not None


@pytest.mark.asyncio
async def test_publisher_persists_expiry_invalidation_and_unverified_recovery(
    database: Database,
    settings: Settings,
) -> None:
    proposal_id = await _scheduled_proposal(database, settings)
    enabled_settings = settings.model_copy(update={"publishing_enabled": True})
    adapter = FakeYouTubeAdapter()
    publisher = YouTubeBrowserPublisher(database, enabled_settings, adapter)

    expired = await publisher.prepare_attempt(proposal_id)
    with database.session() as session:
        attempt = session.get(PublishAttempt, expired.attempt_id)
        assert attempt is not None
        attempt.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    with pytest.raises(ValueError, match="expired"):
        await publisher.confirm_schedule(
            expired.attempt_id,
            confirmation_token=expired.confirmation_token,
            confirmation_phrase=expired.confirmation_phrase,
        )
    assert publisher.attempt_status(expired.attempt_id)["status"] == "expired"
    assert adapter.schedule_calls == []

    invalidated = await publisher.prepare_attempt(proposal_id)
    with database.session() as session:
        proposal = session.get(Proposal, proposal_id)
        assert proposal is not None
        proposal.final_caption = f"{proposal.final_caption} changed"
    with pytest.raises(ValueError, match="changed after preparation"):
        await publisher.confirm_schedule(
            invalidated.attempt_id,
            confirmation_token=invalidated.confirmation_token,
            confirmation_phrase=invalidated.confirmation_phrase,
        )
    assert publisher.attempt_status(invalidated.attempt_id)["status"] == "invalidated"
    assert adapter.schedule_calls == []

    adapter.schedule_receipt = BrowserScheduleReceipt(
        submitted=True,
        verified=False,
        detail="Submission may have succeeded; fixture verification was inconclusive.",
    )
    prepared = await publisher.prepare_attempt(proposal_id)
    submitted = await publisher.confirm_schedule(
        prepared.attempt_id,
        confirmation_token=prepared.confirmation_token,
        confirmation_phrase=prepared.confirmation_phrase,
    )
    assert submitted.status == "publish_unverified"
    assert publisher.attempt_status(prepared.attempt_id)["status"] == "submitted_unverified"
    assert len(adapter.schedule_calls) == 1

    adapter.verify_receipt = BrowserScheduleReceipt(
        submitted=True,
        verified=True,
        external_id="Ugrecovered",
        external_url="https://www.youtube.com/post/Ugrecovered",
        detail="Fixture scheduled post recovered.",
    )
    verification = await publisher.verify_scheduled_post(proposal_id)
    assert verification.verified is True
    assert verification.status == "externally_scheduled"
    assert len(adapter.verify_calls) == 1


@pytest.mark.asyncio
async def test_editorial_approval_queue_schedules_once_without_rights_gate(
    database: Database,
    settings: Settings,
) -> None:
    proposal_id = await _scheduled_proposal(database, settings)
    with database.session() as session:
        proposal = session.get(Proposal, proposal_id)
        assert proposal is not None
        proposal.rights_decision = None
        candidate = session.get(CandidateImage, proposal.candidate_image_id)
        assert candidate is not None
        candidate.rights_status = "unknown"

    adapter = FakeYouTubeAdapter()
    enabled_settings = settings.model_copy(update={"publishing_enabled": True})
    publisher = YouTubeBrowserPublisher(database, enabled_settings, adapter)
    queued = publisher.queue_attempt(proposal_id)
    duplicate = publisher.queue_attempt(proposal_id)
    assert queued["id"] == duplicate["id"]
    assert queued["status"] == "queued"
    assert adapter.schedule_calls == []

    result = await publisher.process_queued_attempt(int(queued["id"]))
    assert result.status == "externally_scheduled"
    assert adapter.validate_calls == 1
    assert len(adapter.schedule_calls) == 1
    assert publisher.next_queued_attempt_id() is None


@pytest.mark.asyncio
async def test_blocked_session_attempt_can_resume_without_duplicate_submission(
    database: Database,
    settings: Settings,
) -> None:
    proposal_id = await _scheduled_proposal(database, settings)
    adapter = FakeYouTubeAdapter()
    adapter.session_valid = False
    adapter.session_detail = "Sign in to the Qlob Editor account."
    enabled_settings = settings.model_copy(update={"publishing_enabled": True})
    publisher = YouTubeBrowserPublisher(database, enabled_settings, adapter)
    queued = publisher.queue_attempt(proposal_id)

    with pytest.raises(ValueError, match="Sign in"):
        await publisher.process_queued_attempt(int(queued["id"]))
    assert publisher.attempt_status(int(queued["id"]))["status"] == "blocked_session"
    assert adapter.schedule_calls == []

    adapter.session_valid = True
    assert publisher.requeue_blocked_session_attempts() == 1
    assert publisher.requeue_blocked_session_attempts() == 0
    result = await publisher.process_queued_attempt(int(queued["id"]))

    assert result.status == "externally_scheduled"
    assert len(adapter.schedule_calls) == 1
    assert publisher.attempt_status(int(queued["id"]))["status"] == "verified"


@pytest.mark.asyncio
async def test_persisted_queue_starts_after_application_restart(
    database: Database,
    settings: Settings,
) -> None:
    proposal_id = await _scheduled_proposal(database, settings)
    adapter = FakeYouTubeAdapter()
    enabled_settings = settings.model_copy(update={"publishing_enabled": True})
    publisher = YouTubeBrowserPublisher(database, enabled_settings, adapter)
    queued = publisher.queue_attempt(proposal_id)

    coordinator = PublisherQueueCoordinator(publisher)
    coordinator.start()
    for _ in range(200):
        if publisher.attempt_status(int(queued["id"]))["status"] == "verified":
            break
        await asyncio.sleep(0.01)

    assert publisher.attempt_status(int(queued["id"]))["status"] == "verified"
    assert len(adapter.schedule_calls) == 1
    assert coordinator.status()["queued"] == 0


@pytest.mark.asyncio
async def test_lineup_batch_validates_every_post_before_queueing(
    database: Database,
    settings: Settings,
) -> None:
    first_id, second_id = await _scheduled_proposal_pair(database, settings)
    enabled_settings = settings.model_copy(update={"publishing_enabled": True})
    publisher = YouTubeBrowserPublisher(
        database,
        enabled_settings,
        FakeYouTubeAdapter(),
    )
    with database.session() as session:
        second = session.get(Proposal, second_id)
        assert second is not None
        second.status = "rejected"

    with pytest.raises(ValueError, match="internally scheduled proposal"):
        publisher.queue_attempts([first_id, second_id])

    with database.session() as session:
        attempts = session.scalars(select(PublishAttempt)).all()
    assert attempts == []


@pytest.mark.asyncio
async def test_lineup_batch_is_serialized_and_deduplicated(
    database: Database,
    settings: Settings,
) -> None:
    first_id, second_id = await _scheduled_proposal_pair(database, settings)
    adapter = FakeYouTubeAdapter()
    enabled_settings = settings.model_copy(update={"publishing_enabled": True})
    publisher = YouTubeBrowserPublisher(database, enabled_settings, adapter)
    coordinator = PublisherQueueCoordinator(publisher)

    queued = coordinator.enqueue_many([first_id, second_id, first_id])
    assert len(queued["attempts"]) == 2

    for _ in range(300):
        if coordinator.status()["queued"] == 0 and not coordinator.status()["running"]:
            break
        await asyncio.sleep(0.01)

    assert [post.proposal_id for post in adapter.schedule_calls] == [
        first_id,
        second_id,
    ]
    assert coordinator.status()["queued"] == 0
    assert coordinator.status()["running"] is False


@pytest.mark.asyncio
async def test_lineup_edit_preserves_punctuation_and_supersedes_stale_payload(
    database: Database,
    settings: Settings,
) -> None:
    proposal_id = await _scheduled_proposal(database, settings)
    adapter = FakeYouTubeAdapter()
    enabled_settings = settings.model_copy(update={"publishing_enabled": True})
    publisher = YouTubeBrowserPublisher(database, enabled_settings, adapter)
    queued = publisher.queue_attempt(proposal_id)
    original = ProposalService(database, settings).detail(proposal_id)
    original_slot = datetime.fromisoformat(str(original["scheduled_publish_at"]))
    new_date = original_slot.astimezone(ZoneInfo(settings.timezone)).date() + timedelta(days=2)

    result = await publisher.update_lineup(
        proposal_id,
        final_caption="Wait... what?!",
        new_date=new_date,
    )

    assert result["requeue_proposal_ids"] == [proposal_id]
    assert result["externally_synced"] is False
    assert publisher.attempt_status(int(queued["id"]))["status"] == "superseded"
    updated = ProposalService(database, settings).detail(proposal_id)
    assert updated["final_caption"] == "Wait... what?!"
    assert (
        datetime.fromisoformat(str(updated["scheduled_publish_at"]))
        .astimezone(ZoneInfo(settings.timezone))
        .date()
        == new_date
    )
    assert adapter.edit_calls == []


@pytest.mark.asyncio
async def test_lineup_move_uses_saved_channel_timezone_and_time(
    database: Database,
    settings: Settings,
) -> None:
    proposal_id = await _scheduled_proposal(database, settings)
    with database.session() as session:
        channel = get_channel(session, settings.channel_handle)
        channel.timezone = "America/Vancouver"
        channel.default_post_time = "09:15"
    publisher = YouTubeBrowserPublisher(database, settings, FakeYouTubeAdapter())
    new_date = datetime.now(ZoneInfo("America/Vancouver")).date() + timedelta(days=10)

    await publisher.update_lineup(
        proposal_id,
        final_caption=None,
        new_date=new_date,
    )

    updated = ProposalService(database, settings).detail(proposal_id)
    slot = datetime.fromisoformat(str(updated["scheduled_publish_at"]))
    configured = slot.astimezone(ZoneInfo("America/Vancouver"))
    assert configured.date() == new_date
    assert (configured.hour, configured.minute) == (9, 15)


@pytest.mark.asyncio
async def test_lineup_external_edit_and_remove_are_verified_before_local_commit(
    database: Database,
    settings: Settings,
) -> None:
    proposal_id = await _scheduled_proposal(database, settings)
    adapter = FakeYouTubeAdapter()
    enabled_settings = settings.model_copy(update={"publishing_enabled": True})
    publisher = YouTubeBrowserPublisher(database, enabled_settings, adapter)
    queued = publisher.queue_attempt(proposal_id)
    await publisher.process_queued_attempt(int(queued["id"]))
    before = ProposalService(database, settings).detail(proposal_id)
    old_slot = datetime.fromisoformat(str(before["scheduled_publish_at"]))
    new_date = old_slot.astimezone(ZoneInfo(settings.timezone)).date() + timedelta(days=3)

    changed = await publisher.update_lineup(
        proposal_id,
        final_caption="Would you trust this look?!",
        new_date=new_date,
    )

    assert changed["externally_synced"] is True
    assert len(adapter.edit_calls) == 1
    after = ProposalService(database, settings).detail(proposal_id)
    assert after["final_caption"] == "Would you trust this look?!"
    assert after["status"] == "externally_scheduled"

    removed = await publisher.remove_from_lineup(proposal_id)
    assert removed["externally_synced"] is True
    assert len(adapter.remove_calls) == 1
    final = ProposalService(database, settings).detail(proposal_id)
    assert final["status"] == "cancelled"
    assert final["scheduled_publish_at"] is None


@pytest.mark.asyncio
async def test_lineup_occupied_date_swaps_slots_without_a_daily_conflict(
    database: Database,
    settings: Settings,
) -> None:
    first_id, second_id = await _scheduled_proposal_pair(database, settings)
    publisher = YouTubeBrowserPublisher(database, settings, FakeYouTubeAdapter())
    proposals = ProposalService(database, settings)
    first_before = proposals.detail(first_id)
    second_before = proposals.detail(second_id)
    second_slot = datetime.fromisoformat(str(second_before["scheduled_publish_at"]))
    with database.session() as session:
        second = session.get(Proposal, second_id)
        assert second is not None
        second.scheduled_publish_at = second_slot.replace(hour=11, minute=30).isoformat()
    second_before = proposals.detail(second_id)
    second_date = (
        datetime.fromisoformat(str(second_before["scheduled_publish_at"]))
        .astimezone(ZoneInfo(settings.timezone))
        .date()
    )

    result = await publisher.update_lineup(
        first_id,
        final_caption=None,
        new_date=second_date,
    )

    assert result["swapped_with"] == second_id
    first_after = proposals.detail(first_id)
    second_after = proposals.detail(second_id)
    assert second_after["scheduled_publish_at"] == first_before["scheduled_publish_at"]
    assert (
        datetime.fromisoformat(str(first_after["scheduled_publish_at"]))
        .astimezone(ZoneInfo(settings.timezone))
        .date()
        == second_date
    )
    local_dates = {
        datetime.fromisoformat(str(item["scheduled_publish_at"]))
        .astimezone(ZoneInfo(settings.timezone))
        .date()
        for item in (first_after, second_after)
    }
    assert len(local_dates) == 2


@pytest.mark.asyncio
async def test_unverified_lineup_edit_keeps_local_record_unchanged(
    database: Database,
    settings: Settings,
) -> None:
    proposal_id = await _scheduled_proposal(database, settings)
    adapter = FakeYouTubeAdapter()
    enabled_settings = settings.model_copy(update={"publishing_enabled": True})
    publisher = YouTubeBrowserPublisher(database, enabled_settings, adapter)
    queued = publisher.queue_attempt(proposal_id)
    await publisher.process_queued_attempt(int(queued["id"]))
    before = ProposalService(database, settings).detail(proposal_id)
    adapter.edit_receipt = BrowserMutationReceipt(
        applied=False,
        verified=False,
        detail="Fixture edit was not verified.",
    )

    with pytest.raises(RuntimeError, match="not verified"):
        await publisher.update_lineup(
            proposal_id,
            final_caption="Do not save this?!",
            new_date=None,
        )

    after = ProposalService(database, settings).detail(proposal_id)
    assert after["final_caption"] == before["final_caption"]
    assert after["scheduled_publish_at"] == before["scheduled_publish_at"]


@pytest.mark.asyncio
async def test_overdue_internal_lineup_post_can_move_to_a_future_slot(
    database: Database,
    settings: Settings,
) -> None:
    proposal_id = await _scheduled_proposal(database, settings)
    timezone = ZoneInfo(settings.timezone)
    overdue = datetime.now(timezone) - timedelta(days=1)
    overdue = overdue.replace(hour=10, minute=0, second=0, microsecond=0)
    with database.session() as session:
        proposal = session.get(Proposal, proposal_id)
        assert proposal is not None
        proposal.scheduled_publish_at = overdue.isoformat()

    future_date = datetime.now(timezone).date() + timedelta(days=2)
    publisher = YouTubeBrowserPublisher(database, settings, FakeYouTubeAdapter())
    result = await publisher.update_lineup(
        proposal_id,
        final_caption=None,
        new_date=future_date,
    )

    assert result["requeue_proposal_ids"] == [proposal_id]
    updated = ProposalService(database, settings).detail(proposal_id)
    moved = datetime.fromisoformat(str(updated["scheduled_publish_at"]))
    assert moved.astimezone(timezone).date() == future_date
    assert moved.astimezone(timezone).hour == 10
