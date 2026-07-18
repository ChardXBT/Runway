from __future__ import annotations

import threading
from datetime import UTC, datetime
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
        self._ensure_lock = threading.Lock()
        self._generation_state_lock = threading.Lock()
        self._generation_state: dict[str, object] = {
            "running": False,
            "started_at": None,
            "completed_at": None,
            "detail": None,
        }

    def generation_status(self) -> dict[str, object]:
        with self._generation_state_lock:
            return dict(self._generation_state)

    def _set_generation_status(
        self,
        *,
        running: bool,
        detail: str | None,
        started_at: str | None = None,
        completed_at: str | None = None,
    ) -> None:
        with self._generation_state_lock:
            if started_at is not None:
                self._generation_state["started_at"] = started_at
            self._generation_state.update(
                {
                    "running": running,
                    "completed_at": completed_at,
                    "detail": detail,
                }
            )

    async def ensure_options(
        self,
        *,
        target: int = 5,
        live_discovery: bool = True,
    ) -> dict[str, object]:
        if target < 1 or target > 50:
            raise ValueError("editorial option target must be between 1 and 50")
        if not self._ensure_lock.acquire(blocking=False):
            result = self._result(
                [],
                None,
                "Generation is already running. RunWay will show the option here when it is ready.",
            )
            result["generation"] = self.generation_status()
            return result

        started_at = datetime.now(UTC).isoformat()
        self._set_generation_status(
            running=True,
            detail="Discovering images and generating captions.",
            started_at=started_at,
            completed_at=None,
        )
        generated_ids: list[int] = []
        discovery_result: dict[str, object] | None = None
        detail: str | None = None
        try:
            missing = max(0, target - self._review_count())
            available = self._unused_candidate_count()
            if missing and available:
                generated_ids.extend(await self._generate(min(missing, available)))

            missing = max(0, target - self._review_count())
            if missing and live_discovery:
                if not self.settings.enable_browser_search:
                    detail = (
                        "No unused candidates remain and browser discovery is disabled."
                    )
                else:
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

            if detail is None:
                detail = (
                    "Editorial options are ready."
                    if self._review_count()
                    else (
                        "No usable image candidates were found. "
                        "Try Generate more for a new search."
                    )
                )
            completed_at = datetime.now(UTC).isoformat()
            self._set_generation_status(
                running=False,
                detail=detail,
                completed_at=completed_at,
            )
            result = self._result(generated_ids, discovery_result, detail)
            result["generation"] = self.generation_status()
            return result
        except Exception as exc:
            self._set_generation_status(
                running=False,
                detail=f"Generation stopped: {exc}",
                completed_at=datetime.now(UTC).isoformat(),
            )
            raise
        finally:
            self._ensure_lock.release()

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
