"""A small durable job queue on `publish_jobs` (spec 03 section 3).

Claiming is a short exclusive transaction. On SQLite (local profile, exactly
one worker) that is ``BEGIN IMMEDIATE``; on Postgres it is ``SELECT ... FOR
UPDATE SKIP LOCKED`` so several workers never claim the same row.

A claim takes a lease (`locked_by`, `locked_until`). A worker that dies leaves
an expired lease behind. Expired jobs are recovered at the next claim, except
`send` jobs: a send whose worker vanished may already have reached the
platform, so it is handed to the expired-send hook (by default a `reconcile`
job) instead of being run again (invariant 4).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Collection, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from runway.db.base import Database
from runway.db.models import DEFAULT_WORKSPACE_ID, PublishJob, utcnow
from runway.domain.state_machine import StateMachine

logger = logging.getLogger(__name__)

SEND = "send"
RECONCILE = "reconcile"

# A send job is never retried by the queue (the engine re-releases instead), so
# it is enqueued with a single attempt. The reconcile job an expired send
# leaves behind only reads, so it gets the read-job retry budget. Kept equal to
# runway.posts.jobs.READ_JOB_MAX_ATTEMPTS without importing runway.posts here;
# tests/unit/test_job_queue.py pins the two together.
RECONCILE_MAX_ATTEMPTS = 3

DEFAULT_LEASE_SECONDS = 300
# Retry delays after the 1st, 2nd, 3rd... failed attempt; the last one repeats.
RETRY_BACKOFF_SECONDS: tuple[int, ...] = (60, 300, 1200)
_ERROR_LIMIT = 2000


class JobState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    # A send whose lease expired; a reconcile job decides what happened.
    RECONCILING = "reconciling"
    CANCELLED = "cancelled"


_J = JobState
JOB_STATE_MACHINE: StateMachine[JobState] = StateMachine(
    "publish job",
    JobState,
    {
        _J.PENDING: frozenset({_J.RUNNING, _J.CANCELLED}),
        _J.RUNNING: frozenset({_J.COMPLETED, _J.PENDING, _J.FAILED, _J.RECONCILING}),
        _J.RECONCILING: frozenset({_J.COMPLETED, _J.FAILED, _J.PENDING}),
        _J.COMPLETED: frozenset(),
        _J.FAILED: frozenset(),
        _J.CANCELLED: frozenset(),
    },
    initial=frozenset({_J.PENDING}),
    column="state",
)


class UndecodableJob(ValueError):
    """A job row whose stored payload cannot be turned into a Job."""


# A claim fails at most this many corrupt rows before giving up for this round.
_MAX_UNDECODABLE_PER_CLAIM = 20


class LeaseLost(RuntimeError):
    """The job is no longer leased to this worker (expired and recovered)."""


@dataclass(frozen=True, slots=True)
class Job:
    id: int
    kind: str
    workspace_id: int
    target_id: int | None
    state: str
    attempts: int
    max_attempts: int
    run_at: datetime
    idempotency_key: str
    payload: dict[str, Any]
    last_error: str | None

    @classmethod
    def of(cls, row: PublishJob) -> Job:
        try:
            payload = json.loads(row.payload_json or "{}")
        except json.JSONDecodeError as exc:
            raise UndecodableJob(f"payload_json is not valid JSON ({exc.msg})") from exc
        if not isinstance(payload, dict):
            raise UndecodableJob("payload_json is not a JSON object")
        return cls(
            id=row.id,
            kind=row.kind,
            workspace_id=row.workspace_id,
            target_id=row.target_id,
            state=row.state,
            attempts=row.attempts,
            max_attempts=row.max_attempts,
            run_at=_aware(row.run_at),
            idempotency_key=row.idempotency_key,
            payload=payload,
            last_error=row.last_error,
        )


ExpiredSendHandler = Callable[[Session, PublishJob, datetime], None]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def retry_delay(attempts: int) -> timedelta:
    index = min(max(attempts, 1), len(RETRY_BACKOFF_SECONDS)) - 1
    return timedelta(seconds=RETRY_BACKOFF_SECONDS[index])


def hand_expired_send_to_reconcile(session: Session, job: PublishJob, now: datetime) -> None:
    """Default expired-send hook: never re-run; queue a `reconcile` job instead.

    The publishing engine replaces this with one that re-runs a send whose
    latest attempt provably never reached the request-sent marker.
    """
    JOB_STATE_MACHINE.apply(job, JobState.RECONCILING)
    job.locked_by = None
    job.locked_until = None
    job.last_error = "lease expired during send; outcome unknown"
    key = f"{RECONCILE}:job:{job.id}"
    if session.scalar(select(PublishJob.id).where(PublishJob.idempotency_key == key)) is None:
        session.add(
            PublishJob(
                workspace_id=job.workspace_id,
                target_id=job.target_id,
                kind=RECONCILE,
                run_at=now,
                state=JOB_STATE_MACHINE.start(JobState.PENDING),
                attempts=0,
                max_attempts=RECONCILE_MAX_ATTEMPTS,
                idempotency_key=key,
                payload_json=json.dumps({"send_job_id": job.id}),
            )
        )


def claim_statement(
    dialect: str, now: datetime, kinds: Collection[str]
) -> Select[tuple[PublishJob]]:
    """The claim query: the earliest due pending job of a kind this worker runs."""
    statement = (
        select(PublishJob)
        .where(
            PublishJob.state == JobState.PENDING.value,
            PublishJob.run_at <= now,
            PublishJob.kind.in_(sorted(kinds)),
        )
        .order_by(PublishJob.run_at, PublishJob.id)
        .limit(1)
    )
    if dialect == "postgresql":
        # Postgres (hosted, several workers): skip rows another worker holds.
        return statement.with_for_update(skip_locked=True)
    # SQLite: the surrounding BEGIN IMMEDIATE already excludes other writers.
    return statement


class JobQueue:
    def __init__(
        self,
        database: Database,
        *,
        clock: Callable[[], datetime] = utcnow,
        expired_send_handler: ExpiredSendHandler = hand_expired_send_to_reconcile,
    ) -> None:
        self.database = database
        self.clock = clock
        self.expired_send_handler = expired_send_handler

    @property
    def dialect(self) -> str:
        return str(self.database.engine.dialect.name)

    @contextmanager
    def _exclusive(self) -> Iterator[Session]:
        """A write transaction that excludes other claimers from its first statement."""
        session = self.database.session_factory()
        try:
            if self.dialect == "sqlite":
                # pysqlite starts transactions lazily and as DEFERRED; take the
                # write lock up front so read-then-update cannot interleave.
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
        finally:
            session.close()

    # ---------------------------------------------------------------- enqueue

    def enqueue(
        self,
        kind: str,
        *,
        idempotency_key: str,
        run_at: datetime | None = None,
        payload: dict[str, Any] | None = None,
        target_id: int | None = None,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
        max_attempts: int = 3,
    ) -> Job:
        """Insert a job, or return the existing one with the same idempotency key."""
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        lookup = select(PublishJob).where(PublishJob.idempotency_key == idempotency_key)
        try:
            with self.database.session() as session:
                existing = session.scalar(lookup)
                if existing is not None:
                    return Job.of(existing)
                row = PublishJob(
                    workspace_id=workspace_id,
                    target_id=target_id,
                    kind=kind,
                    run_at=run_at or self.clock(),
                    state=JOB_STATE_MACHINE.start(JobState.PENDING),
                    attempts=0,
                    max_attempts=max_attempts,
                    idempotency_key=idempotency_key,
                    payload_json=json.dumps(payload or {}, sort_keys=True),
                )
                session.add(row)
                session.flush()
                return Job.of(row)
        except IntegrityError:
            # Another producer inserted the same key between our read and write.
            with self.database.session() as session:
                existing = session.scalar(lookup)
                if existing is None:
                    raise
                return Job.of(existing)

    def enqueue_pending_job(
        self,
        session: Session,
        kind: str,
        *,
        idempotency_key: str,
        run_at: datetime | None = None,
        payload: dict[str, Any] | None = None,
        target_id: int | None = None,
        workspace_id: int = DEFAULT_WORKSPACE_ID,
        max_attempts: int = 3,
    ) -> PublishJob | None:
        """Insert a pending job inside the caller's transaction.

        Same fields as `enqueue`, but no separate session: the row commits or
        rolls back with whatever the caller is doing. Returns None when a job
        with the idempotency key already exists.
        """
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        existing = session.scalar(
            select(PublishJob).where(PublishJob.idempotency_key == idempotency_key)
        )
        if existing is not None:
            return None
        row = PublishJob(
            workspace_id=workspace_id,
            target_id=target_id,
            kind=kind,
            run_at=run_at or self.clock(),
            state=JOB_STATE_MACHINE.start(JobState.PENDING),
            attempts=0,
            max_attempts=max_attempts,
            idempotency_key=idempotency_key,
            payload_json=json.dumps(payload or {}, sort_keys=True),
        )
        session.add(row)
        session.flush()
        return row

    # ------------------------------------------------------------------ claim

    def claim(
        self,
        worker_id: str,
        kinds: Collection[str],
        *,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> Job | None:
        if not kinds:
            return None
        with self._exclusive() as session:
            now = self.clock()
            self._recover_expired(session, now)
            for _ in range(_MAX_UNDECODABLE_PER_CLAIM):
                row = session.scalar(claim_statement(self.dialect, now, kinds))
                if row is None:
                    return None
                JOB_STATE_MACHINE.apply(row, JobState.RUNNING)
                row.attempts += 1
                try:
                    job = Job.of(row)
                except UndecodableJob as exc:
                    # Retrying cannot repair the row; fail it once, with the
                    # reason, instead of tripping over it on every claim.
                    JOB_STATE_MACHINE.apply(row, JobState.FAILED)
                    row.last_error = f"job row cannot be decoded: {exc}"[:_ERROR_LIMIT]
                    session.flush()
                    logger.error("job %s (%s) failed: %s", row.id, row.kind, exc)
                    continue
                row.locked_by = worker_id
                row.locked_until = now + timedelta(seconds=lease_seconds)
                session.flush()
                return replace(job, state=row.state)
            return None

    def _recover_expired(self, session: Session, now: datetime) -> None:
        expired = session.scalars(
            select(PublishJob).where(
                PublishJob.state == JobState.RUNNING.value,
                PublishJob.locked_until < now,
            )
        ).all()
        for job in expired:
            logger.warning("job %s (%s) lease held by %s expired", job.id, job.kind, job.locked_by)
            if job.kind == SEND:
                self.expired_send_handler(session, job, now)
                continue
            job.locked_by = None
            job.locked_until = None
            job.last_error = "lease expired before the job finished"
            if job.attempts >= job.max_attempts:
                JOB_STATE_MACHINE.apply(job, JobState.FAILED)
            else:
                JOB_STATE_MACHINE.apply(job, JobState.PENDING)
                job.run_at = now + retry_delay(job.attempts)

    # ------------------------------------------------------------ lease/finish

    def _leased(self, session: Session, job_id: int, worker_id: str) -> PublishJob:
        row = session.get(PublishJob, job_id)
        if row is None or row.state != JobState.RUNNING.value or row.locked_by != worker_id:
            raise LeaseLost(f"job {job_id} is not leased to {worker_id}")
        return row

    def extend_lease(
        self, job_id: int, worker_id: str, *, lease_seconds: int = DEFAULT_LEASE_SECONDS
    ) -> None:
        with self._exclusive() as session:
            row = self._leased(session, job_id, worker_id)
            row.locked_until = self.clock() + timedelta(seconds=lease_seconds)

    def complete(self, job_id: int, worker_id: str) -> None:
        with self._exclusive() as session:
            row = self._leased(session, job_id, worker_id)
            JOB_STATE_MACHINE.apply(row, JobState.COMPLETED)
            row.locked_by = None
            row.locked_until = None
            row.last_error = None

    def fail(self, job_id: int, worker_id: str, error: str, *, retryable: bool = True) -> JobState:
        """Record a failed attempt; retry later with backoff until max_attempts."""
        with self._exclusive() as session:
            row = self._leased(session, job_id, worker_id)
            row.locked_by = None
            row.locked_until = None
            row.last_error = error[:_ERROR_LIMIT]
            if retryable and row.attempts < row.max_attempts:
                row.run_at = self.clock() + retry_delay(row.attempts)
                return JOB_STATE_MACHINE.apply(row, JobState.PENDING)
            return JOB_STATE_MACHINE.apply(row, JobState.FAILED)

    def defer(self, job_id: int, worker_id: str, run_at: datetime, *, reason: str) -> None:
        """Put a running job back to `run_at` without counting the attempt.

        For a handler that decided "not yet" before doing any work (publishing
        paused, an Instagram container made ahead of `planned_at`).
        """
        with self._exclusive() as session:
            row = self._leased(session, job_id, worker_id)
            JOB_STATE_MACHINE.apply(row, JobState.PENDING)
            row.attempts = max(row.attempts - 1, 0)
            row.run_at = run_at
            row.locked_by = None
            row.locked_until = None
            row.last_error = reason[:_ERROR_LIMIT]

    # ---------------------------------------------------------------- reading

    def get(self, job_id: int) -> Job | None:
        with self.database.session() as session:
            row = session.get(PublishJob, job_id)
            return Job.of(row) if row is not None else None

    def next_run_at(self, kinds: Collection[str]) -> datetime | None:
        """When the earliest pending job (or expiring lease) of these kinds is due."""
        if not kinds:
            return None
        with self.database.session() as session:
            pending = session.scalar(
                select(func.min(PublishJob.run_at)).where(
                    PublishJob.state == JobState.PENDING.value,
                    PublishJob.kind.in_(sorted(kinds)),
                )
            )
            lease = session.scalar(
                select(func.min(PublishJob.locked_until)).where(
                    PublishJob.state == JobState.RUNNING.value
                )
            )
        candidates = [_aware(value) for value in (pending, lease) if value is not None]
        return min(candidates) if candidates else None

    def unknown_pending_counts(self, known_kinds: Collection[str]) -> dict[str, int]:
        """Pending-job counts grouped by kinds this worker does not handle.

        The rows are read-only: an unknown kind may belong to a newer worker
        during a rolling deploy, so this worker must not fail, re-time or claim
        it. The counts are for operator surfaces (worker log and `runway jobs unknown`).
        """
        statement = (
            select(PublishJob.kind, func.count(PublishJob.id))
            .where(PublishJob.state == JobState.PENDING.value)
            .group_by(PublishJob.kind)
        )
        known = set(known_kinds)
        if known:
            statement = statement.where(PublishJob.kind.not_in(sorted(known)))
        with self.database.session() as session:
            rows = session.execute(statement).all()
        return {kind: int(count) for kind, count in sorted(rows, key=lambda row: row[0])}
