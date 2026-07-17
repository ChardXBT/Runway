from __future__ import annotations

import asyncio
import threading

from sqlalchemy import func, select

from leeway.db.models import Proposal, PublishAttempt
from leeway.domain.enums import ProposalStatus
from leeway.publishing.youtube import YouTubeBrowserPublisher


class PublisherQueueCoordinator:
    """Run persisted YouTube scheduling attempts one at a time."""

    def __init__(self, publisher: YouTubeBrowserPublisher):
        self.publisher = publisher
        self._guard = threading.Lock()
        self._worker: threading.Thread | None = None
        self._paused_reason: str | None = None

    def enqueue(self, proposal_id: int) -> dict[str, object]:
        attempt = self.publisher.queue_attempt(proposal_id)
        self._start_if_ready()
        return {
            "attempt": attempt,
            **self.status(),
        }

    def start(self) -> dict[str, object]:
        """Resume persisted queued work after an application restart."""
        self._start_if_ready()
        return self.status()

    def resume(self) -> dict[str, object]:
        requeued = self.publisher.requeue_blocked_session_attempts()
        with self._guard:
            self._paused_reason = None
        self._start_if_ready()
        return {
            **self.status(),
            "requeued": requeued,
        }

    def status(self) -> dict[str, object]:
        with self._guard:
            running = self._worker is not None and self._worker.is_alive()
            paused_reason = self._paused_reason
        queued, blocked_reason = self._persisted_queue_state()
        effective_reason = paused_reason or blocked_reason
        return {
            "running": running,
            "queued": queued,
            "paused": effective_reason is not None,
            "paused_reason": effective_reason,
        }

    def _start_if_ready(self) -> None:
        with self._guard:
            if self._paused_reason is not None:
                return
            if self._worker is not None and self._worker.is_alive():
                return
            if self.publisher.next_queued_attempt_id() is None:
                return
            self._worker = threading.Thread(
                target=self._run,
                name="leeway-youtube-publisher",
                daemon=True,
            )
            self._worker.start()

    def _run(self) -> None:
        try:
            while True:
                attempt_id = self.publisher.next_queued_attempt_id()
                if attempt_id is None:
                    return
                try:
                    asyncio.run(self.publisher.process_queued_attempt(attempt_id))
                except Exception as exc:
                    with self._guard:
                        self._paused_reason = f"{type(exc).__name__}: {exc}"
                    return
        finally:
            with self._guard:
                self._worker = None
                may_restart = self._paused_reason is None
            if may_restart:
                self._start_if_ready()

    def _persisted_queue_state(self) -> tuple[int, str | None]:
        with self.publisher.database.session() as session:
            value = session.scalar(
                select(func.count(PublishAttempt.id)).where(
                    PublishAttempt.publisher == self.publisher.publisher_name,
                    PublishAttempt.status == "queued",
                )
            )
            blocked = session.scalars(
                select(PublishAttempt)
                .where(
                    PublishAttempt.publisher == self.publisher.publisher_name,
                    PublishAttempt.status == "blocked_session",
                )
                .order_by(PublishAttempt.id.desc())
            ).all()
            for attempt in blocked:
                proposal = session.get(Proposal, attempt.proposal_id)
                latest_attempt_id = session.scalar(
                    select(PublishAttempt.id)
                    .where(
                        PublishAttempt.proposal_id == attempt.proposal_id,
                        PublishAttempt.publisher == self.publisher.publisher_name,
                    )
                    .order_by(PublishAttempt.id.desc())
                    .limit(1)
                )
                if (
                    latest_attempt_id == attempt.id
                    and proposal is not None
                    and proposal.status == ProposalStatus.INTERNALLY_SCHEDULED.value
                ):
                    return int(value or 0), (
                        attempt.error_summary
                        or "The publisher session needs attention before scheduling can continue."
                    )
            return int(value or 0), None
