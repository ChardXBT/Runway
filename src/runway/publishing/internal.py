from __future__ import annotations

import json

from sqlalchemy import select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import CandidateImage, MediaAsset, Proposal, ProposalEvent
from runway.db.repositories import audit
from runway.domain.enums import ProposalStatus
from runway.domain.state_machine import require_transition
from runway.publishing.base import (
    PreparedPost,
    PublisherSessionStatus,
    PublishResult,
    VerificationResult,
)


class InternalPublisher:
    """A local state transition only. It opens no browser and performs no network I/O."""

    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    async def validate_session(self) -> PublisherSessionStatus:
        return PublisherSessionStatus(
            valid=True,
            publisher="internal",
            detail="Internal scheduling is local-only; live YouTube publishing is disabled.",
        )

    async def prepare_post(self, proposal_id: int) -> PreparedPost:
        with self.database.session() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                raise LookupError(f"proposal {proposal_id} not found")
            if proposal.status != ProposalStatus.APPROVED.value:
                raise ValueError("only an approved proposal can be prepared")
            candidate = session.get(CandidateImage, proposal.candidate_image_id)
            media = session.get(MediaAsset, candidate.media_asset_id) if candidate else None
            if media is None:
                raise ValueError("approved proposal has no local image")
            scheduled_at = proposal.scheduled_publish_at
            if not scheduled_at:
                raise ValueError("approved proposal has no assigned daily schedule slot")
            return PreparedPost(
                proposal_id=proposal.id,
                planned_publish_at=scheduled_at,
                caption=proposal.final_caption,
                local_image_path=str(self.settings.resolved_data_dir / media.local_path),
            )

    async def schedule_post(self, proposal_id: int) -> PublishResult:
        await self.prepare_post(proposal_id)
        with self.database.session() as session:
            proposal = session.get(Proposal, proposal_id)
            if proposal is None:
                raise LookupError(f"proposal {proposal_id} not found")
            require_transition(proposal.status, ProposalStatus.INTERNALLY_SCHEDULED)
            old = proposal.status
            proposal.status = ProposalStatus.INTERNALLY_SCHEDULED.value
            session.add(
                ProposalEvent(
                    proposal_id=proposal.id,
                    event_type="internally_scheduled",
                    old_value_json=json.dumps({"status": old}),
                    new_value_json=json.dumps(
                        {"status": ProposalStatus.INTERNALLY_SCHEDULED.value}
                    ),
                )
            )
            audit(
                session,
                "proposal_internally_scheduled",
                "proposal",
                proposal.id,
                {"network_action": False},
            )
        return PublishResult(
            proposal_id=proposal_id,
            status=ProposalStatus.INTERNALLY_SCHEDULED.value,
            detail="Recorded internally. Nothing was sent to YouTube.",
        )

    async def verify_scheduled_post(self, proposal_id: int) -> VerificationResult:
        with self.database.session() as session:
            proposal = session.scalar(select(Proposal).where(Proposal.id == proposal_id))
            if proposal is None:
                raise LookupError(f"proposal {proposal_id} not found")
            verified = proposal.status == ProposalStatus.INTERNALLY_SCHEDULED.value
            return VerificationResult(
                proposal_id=proposal.id,
                verified=verified,
                status=proposal.status,
                detail=(
                    "Internal schedule state is persisted; no external post exists."
                    if verified
                    else "Proposal is not internally scheduled."
                ),
            )
