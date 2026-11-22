"""Hourly `purge` job: expired idempotency keys (24 h) and events (7 days)."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from runway.db.base import Database
from runway.events.log import purge_expired_events
from runway.jobs.queue import Job, JobQueue

logger = logging.getLogger(__name__)

PURGE_JOB = "purge"
PURGE_INTERVAL = timedelta(hours=1)


class RetentionPurgeScheduler:
    """Producer of one `purge` job per hour (idempotent per hour slot)."""

    def __init__(self, queue: JobQueue, interval: timedelta = PURGE_INTERVAL) -> None:
        self.queue = queue
        self.interval = interval

    def _slot(self, now: datetime) -> int:
        return int(now.timestamp() // self.interval.total_seconds())

    def next_due(self, now: datetime) -> datetime:
        return datetime.fromtimestamp((self._slot(now) + 1) * self.interval.total_seconds(), tz=UTC)

    def produce(self, now: datetime) -> None:
        self.queue.enqueue(
            PURGE_JOB,
            idempotency_key=f"{PURGE_JOB}:{self._slot(now)}",
            run_at=now,
            max_attempts=1,
        )


def run_purge_job(database: Database, _job: Job) -> None:
    # Imported here: runway.api.v1 is an API package, the worker only needs its store.
    from runway.api.v1.idempotency import IdempotencyStore

    with database.session() as session:
        keys = IdempotencyStore(database).purge_expired(session)
        events = purge_expired_events(session)
    logger.info("retention purge removed %s idempotency keys and %s events", keys, events)
