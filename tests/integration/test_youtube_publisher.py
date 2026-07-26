from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from runway.analysis.service import AnalysisService
from runway.api.proposal_routes import build_proposal_router
from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import AuditEvent, CandidateImage, MediaAsset, Proposal, PublishAttempt
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
    PlaywrightYouTubeAdapter,
    YouTubeBrowserPublisher,
)


class FakeYouTubeAdapter:
    def __init__(self) -> None:
        self.validate_calls = 0
        self.session_valid = True
        self.session_detail = "Qlob Editor fixture session valid."
        self.channel_access_calls: list[str] = []
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

    async def validate_channel_access(
        self,
        channel_url: str,
    ) -> PublisherSessionStatus:
        self.channel_access_calls.append(channel_url)
        return await self.validate_session()

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


def _authorized_settings(settings: Settings) -> Settings:
    return settings.model_copy(
        update={
            "publishing_mode": "authorized_browser",
            "publishing_enabled": True,
            "youtube_automation_authorized": True,
        }
    )


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
    with pytest.raises(ValueError, match="RUNWAY_PUBLISHING_MODE"):
        await disabled.prepare_attempt(proposal_id)
    assert adapter.validate_calls == 0
    assert adapter.schedule_calls == []

    enabled_settings = _authorized_settings(settings)
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


def test_authorized_browser_mode_requires_both_independent_interlocks(
    settings: Settings,
) -> None:
    for update in (
        {"publishing_mode": "authorized_browser"},
        {"publishing_mode": "authorized_browser", "publishing_enabled": True},
        {
            "publishing_mode": "authorized_browser",
            "youtube_automation_authorized": True,
        },
    ):
        with pytest.raises(ValueError, match="authorized_browser requires"):
            Settings(data_dir=settings.data_dir, **update)


def test_channel_timestamp_validation_rejects_naive_and_dst_gap_values() -> None:
    timezone = ZoneInfo("America/Toronto")
    with pytest.raises(ValueError, match="explicit UTC offset"):
        YouTubeBrowserPublisher._validate_channel_timestamp(
            datetime(2026, 8, 9, 13, 30),
            timezone,
        )
    with pytest.raises(ValueError, match="correct offset"):
        YouTubeBrowserPublisher._validate_channel_timestamp(
            datetime.fromisoformat("2026-03-08T02:30:00-05:00"),
            timezone,
        )

    repeated_hour = YouTubeBrowserPublisher._validate_channel_timestamp(
        datetime.fromisoformat("2026-11-01T01:30:00-05:00"),
        timezone,
    )
    assert repeated_hour.isoformat() == "2026-11-01T01:30:00-05:00"


@pytest.mark.asyncio
async def test_editorial_accept_stays_local_and_never_creates_publisher_work(
    database: Database,
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    proposal_id = await _scheduled_proposal(database, settings)
    with database.session() as session:
        proposal = session.get(Proposal, proposal_id)
        assert proposal is not None
        proposal.status = "needs_review"
        proposal.scheduled_publish_at = None

    def forbidden(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("editorial acceptance must not create publisher work")

    monkeypatch.setattr(PublisherQueueCoordinator, "enqueue", forbidden)
    monkeypatch.setattr(PublisherQueueCoordinator, "enqueue_many", forbidden)
    monkeypatch.setattr(YouTubeBrowserPublisher, "validate_session", forbidden)

    application = FastAPI()
    application.include_router(build_proposal_router(database, settings))
    detail = ProposalService(database, settings).detail(proposal_id)
    with TestClient(application) as client:
        response = client.post(
            f"/api/editorial/proposals/{proposal_id}/approve",
            json={"final_caption": detail["final_caption"]},
        )

    assert response.status_code == 200
    assert response.json()["detail"] == (
        "Accepted and added to Lineup. Nothing was sent to YouTube."
    )
    assert response.json()["proposal"]["status"] == "internally_scheduled"
    with database.session() as session:
        assert session.scalars(select(PublishAttempt)).all() == []


@pytest.mark.asyncio
async def test_assisted_preparation_is_offline_and_rejects_a_corrupt_image(
    database: Database,
    settings: Settings,
) -> None:
    proposal_id = await _scheduled_proposal(database, settings)
    adapter = FakeYouTubeAdapter()
    publisher = YouTubeBrowserPublisher(database, settings, adapter)
    with database.session() as session:
        proposal = session.get(Proposal, proposal_id)
        assert proposal is not None
        candidate = session.get(CandidateImage, proposal.candidate_image_id)
        assert candidate is not None
        candidate.rights_status = "unknown"
        proposal.rights_decision = None

    workspace = publisher.prepare_assisted_batch([proposal_id])

    assert workspace.mode == "assisted"
    assert [item.proposal_id for item in workspace.items] == [proposal_id]
    assert workspace.items[0].image_url.startswith("/media/")
    assert any("Rights status is unknown" in item for item in workspace.items[0].warnings)
    assert adapter.validate_calls == 0
    assert adapter.schedule_calls == []
    with database.session() as session:
        assert session.scalars(select(PublishAttempt)).all() == []
        proposal = session.get(Proposal, proposal_id)
        assert proposal is not None
        candidate = session.get(CandidateImage, proposal.candidate_image_id)
        assert candidate is not None
        media = session.get(MediaAsset, candidate.media_asset_id)
        assert media is not None
        image_path = settings.resolved_data_dir / media.local_path
        media_id = media.id
        candidate_id = candidate.id
        original_local_path = media.local_path

    original_bytes = image_path.read_bytes()
    image_path.write_bytes(b"")
    with pytest.raises(ValueError, match="image is empty"):
        publisher.prepare_assisted_batch([proposal_id])
    image_path.write_bytes(b"not a real image")
    with pytest.raises(ValueError, match="valid image"):
        publisher.prepare_assisted_batch([proposal_id])
    image_path.write_bytes(original_bytes)

    unsupported_path = image_path.with_suffix(".txt")
    unsupported_path.write_bytes(original_bytes)
    with database.session() as session:
        media = session.get(MediaAsset, media_id)
        assert media is not None
        media.local_path = str(unsupported_path.relative_to(settings.resolved_data_dir))
    with pytest.raises(ValueError, match="JPG, PNG, GIF, or WEBP"):
        publisher.prepare_assisted_batch([proposal_id])
    with database.session() as session:
        media = session.get(MediaAsset, media_id)
        assert media is not None
        media.local_path = original_local_path

    image_path.write_bytes(b"0" * (16 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="16 MB"):
        publisher.prepare_assisted_batch([proposal_id])
    image_path.write_bytes(original_bytes)
    image_path.unlink()
    with pytest.raises(ValueError, match="missing from local storage"):
        publisher.prepare_assisted_batch([proposal_id])
    image_path.write_bytes(original_bytes)

    with database.session() as session:
        candidate = session.get(CandidateImage, candidate_id)
        assert candidate is not None
        candidate.rights_status = "blocked"
    with pytest.raises(ValueError, match="blocked image"):
        publisher.prepare_assisted_batch([proposal_id])


@pytest.mark.asyncio
async def test_publisher_connection_status_is_durable_and_becomes_stale(
    database: Database,
    settings: Settings,
) -> None:
    adapter = FakeYouTubeAdapter()
    enabled_settings = _authorized_settings(settings)
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
    enabled_settings = _authorized_settings(settings)
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
    enabled_settings = _authorized_settings(settings)
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
    enabled_settings = _authorized_settings(settings)
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
    enabled_settings = _authorized_settings(settings)
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_id, second_id = await _scheduled_proposal_pair(database, settings)
    enabled_settings = _authorized_settings(settings)
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

    with database.session() as session:
        second = session.get(Proposal, second_id)
        assert second is not None
        second.status = "internally_scheduled"

    original_queue = publisher._queue_preflight_in_session
    queue_calls = 0

    def fail_second_queue(*args: object, **kwargs: object) -> PublishAttempt:
        nonlocal queue_calls
        attempt = original_queue(*args, **kwargs)
        queue_calls += 1
        if queue_calls == 2:
            raise RuntimeError("simulated second insert failure")
        return attempt

    monkeypatch.setattr(publisher, "_queue_preflight_in_session", fail_second_queue)
    with pytest.raises(RuntimeError, match="second insert failure"):
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
    enabled_settings = _authorized_settings(settings)
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
    enabled_settings = _authorized_settings(settings)
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

    assert result["requeue_proposal_ids"] == []
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
async def test_lineup_move_accepts_a_full_channel_timezone_timestamp(
    database: Database,
    settings: Settings,
) -> None:
    proposal_id = await _scheduled_proposal(database, settings)
    with database.session() as session:
        channel = get_channel(session, settings.channel_handle)
        channel.timezone = "America/Vancouver"
    publisher = YouTubeBrowserPublisher(database, settings, FakeYouTubeAdapter())
    new_date = datetime.now(ZoneInfo("America/Vancouver")).date() + timedelta(days=10)
    new_timestamp = datetime(
        new_date.year,
        new_date.month,
        new_date.day,
        14,
        45,
        tzinfo=ZoneInfo("America/Vancouver"),
    )

    await publisher.update_lineup(
        proposal_id,
        final_caption=None,
        new_scheduled_publish_at=new_timestamp,
    )

    updated = ProposalService(database, settings).detail(proposal_id)
    slot = datetime.fromisoformat(str(updated["scheduled_publish_at"]))
    configured = slot.astimezone(ZoneInfo("America/Vancouver"))
    assert configured.date() == new_date
    assert (configured.hour, configured.minute) == (14, 45)


@pytest.mark.asyncio
async def test_confirmed_external_posts_are_immutable_in_normal_lineup_actions(
    database: Database,
    settings: Settings,
) -> None:
    proposal_id = await _scheduled_proposal(database, settings)
    adapter = FakeYouTubeAdapter()
    enabled_settings = _authorized_settings(settings)
    publisher = YouTubeBrowserPublisher(database, enabled_settings, adapter)
    queued = publisher.queue_attempt(proposal_id)
    await publisher.process_queued_attempt(int(queued["id"]))
    before = ProposalService(database, settings).detail(proposal_id)
    with pytest.raises(ValueError, match="immutable"):
        await publisher.update_lineup(
            proposal_id,
            final_caption="Would you trust this look?!",
            new_date=None,
        )
    with pytest.raises(ValueError, match="immutable"):
        await publisher.remove_from_lineup(proposal_id)

    assert adapter.edit_calls == []
    assert adapter.remove_calls == []
    after = ProposalService(database, settings).detail(proposal_id)
    assert after == before


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
async def test_lineup_move_rejects_multiple_existing_posts_on_target_date(
    database: Database,
    settings: Settings,
) -> None:
    first_id, second_id = await _scheduled_proposal_pair(database, settings)
    proposals = ProposalService(database, settings)
    first_before = proposals.detail(first_id)
    second_before = proposals.detail(second_id)
    second_slot = datetime.fromisoformat(str(second_before["scheduled_publish_at"]))

    with database.session() as session:
        second = session.get(Proposal, second_id)
        assert second is not None
        copied_fields = {
            column.name: getattr(second, column.name)
            for column in Proposal.__table__.columns
            if column.name not in {"id", "created_at", "updated_at"}
        }
        copied_fields["planned_publish_at"] = (
            datetime.fromisoformat(second.planned_publish_at) + timedelta(hours=1)
        ).isoformat()
        copied_fields["scheduled_publish_at"] = (second_slot + timedelta(hours=1)).isoformat()
        session.add(Proposal(**copied_fields))

    target_date = second_slot.astimezone(ZoneInfo(settings.timezone)).date()
    publisher = YouTubeBrowserPublisher(database, settings, FakeYouTubeAdapter())
    with pytest.raises(ValueError, match="multiple active RunWay posts"):
        await publisher.update_lineup(
            first_id,
            final_caption=None,
            new_date=target_date,
        )

    assert (
        proposals.detail(first_id)["scheduled_publish_at"] == first_before["scheduled_publish_at"]
    )


@pytest.mark.asyncio
async def test_unverified_lineup_edit_is_blocked_and_keeps_local_record_unchanged(
    database: Database,
    settings: Settings,
) -> None:
    proposal_id = await _scheduled_proposal(database, settings)
    adapter = FakeYouTubeAdapter()
    enabled_settings = _authorized_settings(settings)
    publisher = YouTubeBrowserPublisher(database, enabled_settings, adapter)
    queued = publisher.queue_attempt(proposal_id)
    await publisher.process_queued_attempt(int(queued["id"]))
    before = ProposalService(database, settings).detail(proposal_id)
    with database.session() as session:
        proposal = session.get(Proposal, proposal_id)
        assert proposal is not None
        proposal.status = "publish_unverified"

    with pytest.raises(ValueError, match="unverified"):
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

    assert result["requeue_proposal_ids"] == []
    updated = ProposalService(database, settings).detail(proposal_id)
    moved = datetime.fromisoformat(str(updated["scheduled_publish_at"]))
    assert moved.astimezone(timezone).date() == future_date
    assert moved.astimezone(timezone).hour == 10


@pytest.mark.asyncio
async def test_connector_check_works_while_disabled_without_arming_saved_session(
    database: Database,
    settings: Settings,
) -> None:
    adapter = FakeYouTubeAdapter()
    disabled = YouTubeBrowserPublisher(database, settings, adapter)

    status = await disabled.validate_channel_access(settings.publisher_channel_url)

    assert status.valid is True
    assert adapter.channel_access_calls == [settings.publisher_channel_url]
    enabled = YouTubeBrowserPublisher(database, _authorized_settings(settings), adapter)
    assert enabled.connection_status()["state"] == "unchecked"


@pytest.mark.asyncio
async def test_connector_rejects_a_different_managed_channel(
    database: Database,
    settings: Settings,
) -> None:
    publisher = YouTubeBrowserPublisher(database, settings, FakeYouTubeAdapter())

    with pytest.raises(ValueError, match="pinned to Qlob"):
        await publisher.validate_channel_access("https://www.youtube.com/@another-channel")


def test_loaded_channel_identity_requires_an_exact_path_segment(settings: Settings) -> None:
    adapter = PlaywrightYouTubeAdapter(settings)

    assert adapter._configured_channel_loaded(SimpleNamespace(url=settings.publisher_channel_url))
    assert adapter._configured_channel_loaded(
        SimpleNamespace(url="https://www.youtube.com/@Qlob/posts")
    )
    assert not adapter._configured_channel_loaded(
        SimpleNamespace(
            url=(f"https://www.youtube.com/channel/not-{settings.publisher_channel_id}/posts")
        )
    )
    assert not adapter._configured_channel_loaded(
        SimpleNamespace(url="https://www.youtube.com/@Qlob-imposter/posts")
    )


def test_saved_session_status_is_scoped_to_the_configured_channel(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        session.add(
            AuditEvent(
                event_type="youtube_session_validated",
                entity_type="publisher",
                entity_id=None,
                details_json=(
                    '{"valid": true, "publisher": "fixture", '
                    '"detail": "Wrong channel", "checks": {}, '
                    '"checked_channel_id": "UC-not-qLOB"}'
                ),
            )
        )

    publisher = YouTubeBrowserPublisher(
        database,
        _authorized_settings(settings),
        FakeYouTubeAdapter(),
    )

    assert publisher.connection_status()["state"] == "unchecked"


@pytest.mark.asyncio
async def test_persisted_session_block_prevents_other_queued_work_after_restart(
    database: Database,
    settings: Settings,
) -> None:
    first_id, second_id = await _scheduled_proposal_pair(database, settings)
    adapter = FakeYouTubeAdapter()
    adapter.session_valid = False
    adapter.session_detail = "Sign in to the Qlob Editor account."
    publisher = YouTubeBrowserPublisher(
        database,
        _authorized_settings(settings),
        adapter,
    )
    attempts = publisher.queue_attempts([first_id, second_id])

    with pytest.raises(ValueError, match="Sign in"):
        await publisher.process_queued_attempt(int(attempts[0]["id"]))
    assert publisher.attempt_status(int(attempts[0]["id"]))["status"] == ("blocked_session")
    assert publisher.attempt_status(int(attempts[1]["id"]))["status"] == "queued"

    adapter.session_valid = True
    restarted = PublisherQueueCoordinator(publisher)
    status = restarted.start()
    await asyncio.sleep(0.05)

    assert status["paused"] is True
    assert publisher.attempt_status(int(attempts[1]["id"]))["status"] == "queued"
    assert adapter.schedule_calls == []


def test_explicit_empty_lineup_selection_is_never_expanded_to_all_posts(
    database: Database,
    settings: Settings,
) -> None:
    application = FastAPI()
    application.include_router(build_proposal_router(database, settings))

    with TestClient(application) as client:
        response = client.post(
            "/api/lineup/push",
            json={"confirmed": True, "mode": "assisted", "proposal_ids": []},
        )

    assert response.status_code == 422
    assert "select at least one" in response.json()["detail"]


@pytest.mark.asyncio
async def test_waiting_lineup_post_cannot_bypass_batch_confirmation_through_retry(
    database: Database,
    settings: Settings,
) -> None:
    proposal_id = await _scheduled_proposal(database, settings)
    application = FastAPI()
    application.include_router(build_proposal_router(database, settings))

    with TestClient(application) as client:
        response = client.post(f"/api/lineup/{proposal_id}/retry")

    assert response.status_code == 422
    assert "failed-before-submission" in response.json()["detail"]
    with database.session() as session:
        assert session.scalars(select(PublishAttempt)).all() == []


def test_queue_resume_requires_a_fresh_saved_session_check(
    database: Database,
    settings: Settings,
) -> None:
    publisher = YouTubeBrowserPublisher(
        database,
        _authorized_settings(settings),
        FakeYouTubeAdapter(),
    )
    coordinator = PublisherQueueCoordinator(publisher)

    with pytest.raises(ValueError, match="fresh successful saved-session check"):
        coordinator.resume()
