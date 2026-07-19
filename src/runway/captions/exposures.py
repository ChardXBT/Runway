from __future__ import annotations

import json
from datetime import UTC, datetime

from sqlalchemy import select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CaptionCandidateRecord,
    CaptionExposure,
    PairwisePreference,
    Proposal,
)
from runway.db.repositories import get_channel
from runway.intelligence.policies import ChannelPolicyService


class CaptionExposureService:
    interface_version = "runway-generator-v2"

    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    def record_display(self, proposal_id: int) -> CaptionExposure | None:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            proposal = session.scalar(
                select(Proposal).where(
                    Proposal.id == proposal_id,
                    Proposal.channel_id == channel.id,
                )
            )
            if proposal is None:
                raise LookupError(f"proposal {proposal_id} not found")
            if proposal.caption_slate_id is None:
                return None
            existing = session.scalar(
                select(CaptionExposure)
                .where(
                    CaptionExposure.proposal_id == proposal.id,
                    CaptionExposure.caption_slate_id == proposal.caption_slate_id,
                )
                .limit(1)
            )
            if existing is not None:
                return existing
            candidates = session.scalars(
                select(CaptionCandidateRecord)
                .where(
                    CaptionCandidateRecord.caption_slate_id == proposal.caption_slate_id,
                    CaptionCandidateRecord.displayed.is_(True),
                )
                .order_by(
                    CaptionCandidateRecord.display_order,
                    CaptionCandidateRecord.rank,
                )
            ).all()
            exposure = CaptionExposure(
                channel_id=proposal.channel_id,
                proposal_id=proposal.id,
                caption_slate_id=proposal.caption_slate_id,
                candidate_image_id=proposal.candidate_image_id,
                ordered_candidate_ids_json=json.dumps([row.id for row in candidates]),
                interface_version=self.interface_version,
            )
            session.add(exposure)
            session.flush()
            return exposure

    def record_decision(
        self,
        proposal_id: int,
        *,
        decision_type: str,
        final_caption: str | None,
        original_caption: str | None = None,
        reason_codes: list[str] | None = None,
    ) -> None:
        self.record_display(proposal_id)
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            proposal = session.scalar(
                select(Proposal).where(
                    Proposal.id == proposal_id,
                    Proposal.channel_id == channel.id,
                )
            )
            if proposal is None or proposal.caption_slate_id is None:
                return
            channel_id = proposal.channel_id
        policy = ChannelPolicyService(
            self.database,
            self.settings,
        ).ensure_defaults(channel_id)
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            proposal = session.scalar(
                select(Proposal).where(
                    Proposal.id == proposal_id,
                    Proposal.channel_id == channel.id,
                )
            )
            if proposal is None or proposal.caption_slate_id is None:
                return
            exposure = session.scalar(
                select(CaptionExposure)
                .where(
                    CaptionExposure.proposal_id == proposal.id,
                    CaptionExposure.caption_slate_id == proposal.caption_slate_id,
                )
                .limit(1)
            )
            if exposure is None:
                return
            if (
                exposure.decision_type == decision_type
                and exposure.final_caption == final_caption
            ):
                return
            candidate_ids = [
                int(value) for value in json.loads(exposure.ordered_candidate_ids_json)
            ]
            candidates = {
                row.id: row
                for row in session.scalars(
                    select(CaptionCandidateRecord).where(
                        CaptionCandidateRecord.id.in_(candidate_ids)
                    )
                )
            }
            selected = next(
                (
                    row
                    for row in candidates.values()
                    if final_caption is not None and row.text == final_caption.strip()
                ),
                None,
            )
            now = datetime.now(UTC)
            displayed = exposure.displayed_at
            if displayed.tzinfo is None:
                displayed = displayed.replace(tzinfo=UTC)
            latency = max(0.0, (now - displayed).total_seconds() * 1000)
            exposure.selected_candidate_id = selected.id if selected else None
            exposure.final_caption = final_caption
            exposure.decision_type = decision_type
            exposure.decision_latency_ms = round(latency, 3)
            if selected is not None:
                selected.selected = True
                selected.decision_latency_ms = round(latency, 3)
            if decision_type in {"selected", "accepted"} and selected is not None:
                for other in candidates.values():
                    if other.id == selected.id:
                        continue
                    session.add(
                        PairwisePreference(
                            channel_id=proposal.channel_id,
                            proposal_id=proposal.id,
                            candidate_image_id=proposal.candidate_image_id,
                            preferred_candidate_id=selected.id,
                            preferred_text=selected.text,
                            dispreferred_candidate_id=other.id,
                            dispreferred_text=other.text,
                            preference_source=decision_type,
                            label_source="human",
                            strength=1.0,
                            reason_codes_json=json.dumps(reason_codes or []),
                            policy_version=policy.version,
                        )
                    )
            if (
                decision_type == "edited"
                and final_caption
                and original_caption
                and final_caption.strip() != original_caption.strip()
            ):
                if selected is not None:
                    selected.edited = True
                    selected.replacement_text = final_caption.strip()
                original = next(
                    (
                        row
                        for row in candidates.values()
                        if row.text == original_caption.strip()
                    ),
                    None,
                )
                session.add(
                    PairwisePreference(
                        channel_id=proposal.channel_id,
                        proposal_id=proposal.id,
                        candidate_image_id=proposal.candidate_image_id,
                        preferred_candidate_id=None,
                        preferred_text=final_caption.strip(),
                        dispreferred_candidate_id=original.id if original else None,
                        dispreferred_text=original_caption.strip(),
                        preference_source="human_edit",
                        label_source="human",
                        strength=1.25,
                        reason_codes_json=json.dumps(reason_codes or ["human_edit"]),
                        policy_version=policy.version,
                    )
                )
