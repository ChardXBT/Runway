from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from leeway.analysis.service import AnalysisService
from leeway.capture.service import CaptureService
from leeway.config import Settings
from leeway.db.base import Database
from leeway.db.models import Proposal, PublishAttempt
from leeway.discovery.service import DiscoveryService
from leeway.intelligence.profile import StyleProfileService
from leeway.proposals.service import ProposalService
from leeway.publishing.base import PreparedPost, PublisherSessionStatus
from leeway.publishing.internal import InternalPublisher
from leeway.publishing.youtube import (
    BrowserScheduleReceipt,
    YouTubeBrowserPublisher,
)


class FakeYouTubeAdapter:
    def __init__(self) -> None:
        self.validate_calls = 0
        self.schedule_calls: list[PreparedPost] = []
        self.verify_calls: list[PreparedPost] = []
        self.schedule_receipt = BrowserScheduleReceipt(
            submitted=True,
            verified=True,
            external_id="Ugfixture",
            external_url="https://www.youtube.com/post/Ugfixture",
            detail="Fixture scheduled post verified.",
        )
        self.verify_receipt = self.schedule_receipt

    async def validate_session(self) -> PublisherSessionStatus:
        self.validate_calls += 1
        return PublisherSessionStatus(
            valid=True,
            publisher="fixture",
            detail="Qlob Editor fixture session valid.",
        )

    async def schedule(self, post: PreparedPost) -> BrowserScheduleReceipt:
        self.schedule_calls.append(post)
        return self.schedule_receipt

    async def verify(self, post: PreparedPost) -> BrowserScheduleReceipt:
        self.verify_calls.append(post)
        return self.verify_receipt


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
