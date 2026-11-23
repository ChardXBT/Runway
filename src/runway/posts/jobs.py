"""Job kinds of the publishing engine and in-transaction enqueueing (spec 03 section 3).

`send`, `poll_processing`, `verify` and `reconcile` act on one target;
`probe_media` acts on one media asset. A job is inserted in the same
transaction as the state change that needs it (a release and its send job
commit together, or neither does).
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from runway.db.models import PublishJob
from runway.jobs.queue import JOB_STATE_MACHINE, RECONCILE, SEND, JobState

SEND_JOB = SEND
POLL_PROCESSING_JOB = "poll_processing"
VERIFY_JOB = "verify"
RECONCILE_JOB = RECONCILE
PROBE_MEDIA_JOB = "probe_media"

TARGET_JOB_KINDS: frozenset[str] = frozenset(
    {SEND_JOB, POLL_PROCESSING_JOB, VERIFY_JOB, RECONCILE_JOB}
)
PUBLISHING_JOB_KINDS: frozenset[str] = TARGET_JOB_KINDS | {PROBE_MEDIA_JOB}

# A send job is never re-run by the queue's own retry: a failed send is
# classified by the engine, which enqueues a new job for an allowed retry.
SEND_MAX_ATTEMPTS = 1
# Polls, verifications and reconciliations only read; transient errors may repeat.
READ_JOB_MAX_ATTEMPTS = 3

_FINISHED_JOB_STATES = frozenset(
    {JobState.COMPLETED.value, JobState.FAILED.value, JobState.CANCELLED.value}
)


def release_job_key(target_id: int, payload_hash: str) -> str:
    """Spec 03 section 4 step 1: one send job per target per release."""
    return f"{target_id}:{payload_hash}"


def key_in_use(session: Session, key: str) -> bool:
    return (
        session.scalar(select(PublishJob.id).where(PublishJob.idempotency_key == key)) is not None
    )


def free_key(session: Session, base: str) -> str:
    """`base`, or `base:<n>` for the first n not used yet (a later release of the same payload)."""
    if not key_in_use(session, base):
        return base
    suffix = 2
    while key_in_use(session, f"{base}:{suffix}"):
        suffix += 1
    return f"{base}:{suffix}"


def enqueue_in(
    session: Session,
    kind: str,
    *,
    idempotency_key: str,
    run_at: datetime,
    workspace_id: int,
    target_id: int | None = None,
    payload: dict[str, Any] | None = None,
    max_attempts: int = READ_JOB_MAX_ATTEMPTS,
) -> PublishJob:
    """Insert a job in the caller's transaction, or return the one with the same key."""
    existing = session.scalar(
        select(PublishJob).where(PublishJob.idempotency_key == idempotency_key)
    )
    if existing is not None:
        return existing
    row = PublishJob(
        workspace_id=workspace_id,
        target_id=target_id,
        kind=kind,
        run_at=run_at,
        state=JOB_STATE_MACHINE.start(JobState.PENDING),
        attempts=0,
        max_attempts=max_attempts,
        idempotency_key=idempotency_key,
        payload_json=json.dumps(payload or {}, sort_keys=True),
    )
    session.add(row)
    session.flush()
    return row


def pending_target_jobs(
    session: Session, target_id: int, kinds: Iterable[str] = TARGET_JOB_KINDS
) -> list[PublishJob]:
    return list(
        session.scalars(
            select(PublishJob).where(
                PublishJob.target_id == target_id,
                PublishJob.kind.in_(sorted(kinds)),
                PublishJob.state == JobState.PENDING.value,
            )
        ).all()
    )


def cancel_pending_jobs(
    session: Session, target_id: int, kinds: Iterable[str] = TARGET_JOB_KINDS
) -> int:
    """Cancel the target's jobs that have not been claimed yet. Running jobs are left alone:
    the worker re-checks the target's state and release inside its intent transaction."""
    jobs = pending_target_jobs(session, target_id, kinds)
    for job in jobs:
        JOB_STATE_MACHINE.apply(job, JobState.CANCELLED)
        job.last_error = "cancelled: the target changed or was cancelled"
    session.flush()
    return len(jobs)


def job_payload(job: PublishJob) -> dict[str, Any]:
    try:
        value = json.loads(job.payload_json or "{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def job_finished(job: PublishJob) -> bool:
    return job.state in _FINISHED_JOB_STATES
