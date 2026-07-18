from __future__ import annotations

from typing import cast

from sqlalchemy import func, select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import CandidateImage, Proposal
from runway.discovery.service import DiscoveryService
from runway.proposals.service import ProposalService


class EditorialService:
    """Keep the human review conveyor supplied with ranked options."""

    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings
        self.proposals = ProposalService(database, settings)

    async def ensure_options(
        self,
        *,
        target: int = 5,
        live_discovery: bool = True,
    ) -> dict[str, object]:
        if target < 1 or target > 50:
            raise ValueError("editorial option target must be between 1 and 50")
        generated_ids: list[int] = []
        discovery_result: dict[str, object] | None = None

        missing = max(0, target - self._review_count())
        available = self._unused_candidate_count()
        if missing and available:
            generated_ids.extend(await self._generate(min(missing, available)))

        missing = max(0, target - self._review_count())
        if missing and live_discovery:
            if not self.settings.enable_browser_search:
                return self._result(
                    generated_ids,
                    discovery_result,
                    "No unused candidates remain and browser discovery is disabled.",
                )
            discovery_result = await DiscoveryService(
                self.database,
                self.settings,
            ).discover(
                days=missing,
                provider_name="browser",
                live=True,
            )
            available = self._unused_candidate_count()
            if available:
                generated_ids.extend(await self._generate(min(missing, available)))

        detail = (
            "Editorial options are ready."
            if self._review_count()
            else "No accepted image candidates were found."
        )
        return self._result(generated_ids, discovery_result, detail)

    async def _generate(self, count: int) -> list[int]:
        result = await self.proposals.generate_batch(
            days=count,
            start_date=self.proposals.next_generation_date(),
        )
        return [int(value) for value in cast(list[int], result["proposal_ids"])]

    def _review_count(self) -> int:
        with self.database.session() as session:
            return int(
                session.scalar(
                    select(func.count(Proposal.id)).where(Proposal.status == "needs_review")
                )
                or 0
            )

    def _unused_candidate_count(self) -> int:
        with self.database.session() as session:
            return int(
                session.scalar(
                    select(func.count(CandidateImage.id)).where(
                        CandidateImage.hard_rejection_reason.is_(None),
                        CandidateImage.id.not_in(select(Proposal.candidate_image_id)),
                    )
                )
                or 0
            )

    def _result(
        self,
        generated_ids: list[int],
        discovery_result: dict[str, object] | None,
        detail: str,
    ) -> dict[str, object]:
        return {
            "detail": detail,
            "generated_proposal_ids": generated_ids,
            "discovery": discovery_result,
            "next_proposal": self.proposals.next_for_review(),
            "workflow": self.proposals.workflow_summary(),
        }
