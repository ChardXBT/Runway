from __future__ import annotations

import asyncio
import threading

from sqlalchemy import func, select

from runway.db.models import Proposal, PublishAttempt
from runway.domain.enums import ProposalStatus
from runway.publishing.youtube import StalePublishAttempt, YouTubeBrowserPublisher


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

    def enqueue_many(self, proposal_ids: list[int]) -> dict[str, object]:
        """Persist a validated batch before starting the serialized browser worker."""
        attempts = self.publisher.queue_attempts(proposal_ids)
        self._start_if_ready()
        return {
            "attempts": attempts,
            **self.status(),
        }

    def retry_failed(self, proposal_id: int) -> dict[str, object]:
        """Queue one confirmed pre-submission failure and clear only its worker pause."""
        attempt = self.publisher.queue_attempt(
            proposal_id,
            trigger="human_retry_after_failed_before_submission",
        )
        with self._guard:
            self._paused_reason = None
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
        connection = self.publisher.connection_status()
        if connection.get("state") != "connected" or connection.get("valid") is not True:
            raise ValueError(
                "run a fresh successful saved-session check before resuming blocked actions"
            )
        requeued = self.publisher.requeue_blocked_session_attempts()
        if requeued == 0:
            raise ValueError("no session-blocked YouTube actions are available to resume")
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
        queued, blocked_reason, proposal_ids = self._persisted_queue_state()
        effective_reason = paused_reason or blocked_reason
        return {
            "running": running,
            "queued": queued,
            "paused": effective_reason is not None,
            "paused_reason": effective_reason,
            "proposal_ids": proposal_ids,
        }

    def _start_if_ready(self) -> None:
        _queued, blocked_reason, _proposal_ids = self._persisted_queue_state()
        with self._guard:
            if blocked_reason is not None:
                self._paused_reason = blocked_reason
                return
            if self._paused_reason is not None:
                return
            if self._worker is not None and self._worker.is_alive():
                return
            if self.publisher.next_queued_attempt_id() is None:
                return
            self._worker = threading.Thread(
                target=self._run,
                name="runway-youtube-publisher",
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
                except StalePublishAttempt:
                    continue
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

    def _persisted_queue_state(self) -> tuple[int, str | None, list[int]]:
        with self.publisher.database.session() as session:
            active_attempts = session.scalars(
                select(PublishAttempt)
                .where(
                    PublishAttempt.publisher == self.publisher.publisher_name,
                    PublishAttempt.status.in_(["queued", "submitting", "blocked_session"]),
                )
                .order_by(PublishAttempt.id)
            ).all()
            proposal_ids = list(dict.fromkeys(attempt.proposal_id for attempt in active_attempts))
            value = sum(attempt.status == "queued" for attempt in active_attempts)
            latest_ids = (
                select(
                    PublishAttempt.proposal_id.label("proposal_id"),
                    func.max(PublishAttempt.id).label("attempt_id"),
                )
                .where(
                    PublishAttempt.publisher == self.publisher.publisher_name,
                )
                .group_by(PublishAttempt.proposal_id)
                .subquery()
            )
            blocked = session.scalar(
                select(PublishAttempt)
                .join(latest_ids, PublishAttempt.id == latest_ids.c.attempt_id)
                .join(Proposal, Proposal.id == PublishAttempt.proposal_id)
                .where(
                    PublishAttempt.status == "blocked_session",
                    Proposal.status == ProposalStatus.INTERNALLY_SCHEDULED.value,
                )
                .order_by(PublishAttempt.id.desc())
                .limit(1)
            )
            if blocked is not None:
                return (
                    int(value or 0),
                    (
                        blocked.error_summary
                        or "The publisher session needs attention before scheduling can continue."
                    ),
                    proposal_ids,
                )
            return int(value or 0), None, proposal_ids
