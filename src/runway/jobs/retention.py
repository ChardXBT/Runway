"""Hourly `purge` job: keeps the database and the lock directories from growing forever.

- idempotency keys after 24 hours and events after 7 days;
- finished `publish_jobs` after 14 days, `send`/`reconcile` history after 90;
- lock files of accounts and token references that no longer exist, when no
  process holds them.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ConnectedAccount, PublishJob, utcnow
from runway.events.log import purge_expired_events
from runway.jobs.queue import RECONCILE, SEND, Job, JobQueue, JobState
from runway.locks import LockTimeout, exclusive_file_lock
from runway.platforms.sync import sync_lock_path
from runway.platforms.tokens import TokenStore

logger = logging.getLogger(__name__)

PURGE_JOB = "purge"
PURGE_INTERVAL = timedelta(hours=1)
FINISHED_JOB_RETENTION = timedelta(days=14)
# Sends and reconciliations are the evidence for "did this post go out?".
PUBLISHING_JOB_RETENTION = timedelta(days=90)
FINISHED_JOB_STATES = (
    JobState.COMPLETED.value,
    JobState.FAILED.value,
    JobState.CANCELLED.value,
)
_PUBLISHING_KINDS = (SEND, RECONCILE)


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


def purge_finished_jobs(session: Session, *, now: datetime | None = None) -> int:
    moment = now or utcnow()
    removed = 0
    for kinds_clause, retention in (
        (PublishJob.kind.not_in(_PUBLISHING_KINDS), FINISHED_JOB_RETENTION),
        (PublishJob.kind.in_(_PUBLISHING_KINDS), PUBLISHING_JOB_RETENTION),
    ):
        result = session.execute(
            delete(PublishJob).where(
                kinds_clause,
                PublishJob.state.in_(FINISHED_JOB_STATES),
                PublishJob.updated_at < moment - retention,
            )
        )
        removed += int(getattr(result, "rowcount", 0) or 0)
    return removed


def remove_stale_lock_files(directory: Path, keep: Iterable[str]) -> int:
    """Delete `<name>.lock` files whose name is not in `keep` and that nobody holds."""
    if not directory.is_dir():
        return 0
    wanted = set(keep)
    removed = 0
    for path in sorted(directory.glob("*.lock")):
        if path.stem in wanted:
            continue
        try:
            with exclusive_file_lock(path, timeout=0):
                pass
        except LockTimeout:
            continue  # held right now: in use, not stale
        try:
            path.unlink()
        except OSError:
            # Opened again by another process since; it will be retried next hour.
            continue
        removed += 1
    return removed


def run_purge_job(database: Database, settings: Settings, _job: Job) -> None:
    # Imported here: runway.api.v1 is an API package, the worker only needs its store.
    from runway.api.v1.idempotency import IdempotencyStore

    with database.session() as session:
        keys = IdempotencyStore(database).purge_expired(session)
        events = purge_expired_events(session)
        jobs = purge_finished_jobs(session)
        accounts = session.execute(select(ConnectedAccount.id, ConnectedAccount.token_ref)).all()
    token_refs = {str(token_ref) for _id, token_ref in accounts if token_ref}
    sync_names = {sync_lock_path(settings, int(account_id)).stem for account_id, _ in accounts}
    refresh_dir = TokenStore(settings.resolved_data_dir / "secrets").refresh_lock_dir
    locks = remove_stale_lock_files(refresh_dir, token_refs)
    locks += remove_stale_lock_files(sync_lock_path(settings, 0).parent, sync_names)
    logger.info(
        "retention purge removed %s idempotency keys, %s events, %s jobs and %s lock files",
        keys,
        events,
        jobs,
        locks,
    )
