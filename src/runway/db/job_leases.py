from __future__ import annotations

import logging
import os
import threading
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from sqlalchemy import update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from runway.db.base import Database
from runway.db.models import GenerationRun, SearchRun, SyncRun, utcnow
from runway.domain.enums import RunStatus

logger = logging.getLogger(__name__)

DEFAULT_LEASE_DURATION = timedelta(minutes=15)
HEARTBEAT_INTERVAL = timedelta(seconds=30)

JobRun = SearchRun | GenerationRun | SyncRun
JobRunModel = type[SearchRun] | type[GenerationRun] | type[SyncRun]


class LeaseLost(RuntimeError):
    """The database no longer recognizes this worker as the run owner."""


def new_owner_token() -> str:
    return f"{os.getpid()}:{uuid.uuid4().hex}"


def aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def lease_claim_fields(
    owner_token: str,
    *,
    lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    now: datetime | None = None,
) -> dict[str, object]:
    claimed_at = aware_utc(now or utcnow())
    return {
        "owner_token": owner_token,
        "heartbeat_at": claimed_at,
        "lease_expires_at": claimed_at + lease_duration,
        "lease_version": 1,
    }


def run_lease_expired(
    run: JobRun,
    now: datetime,
    *,
    fallback_after: timedelta,
) -> bool:
    """Return whether a running row has no current proof of live ownership.

    Rows written before leases existed have neither owner nor lease fields. Those
    legacy rows retain the historical age-based fallback so an already-active
    pre-upgrade worker is not killed immediately after deployment. New rows are
    controlled by their persisted lease instead of by `started_at`, which lets
    long live jobs continue while recently-started crashed jobs time out.
    """

    current = aware_utc(now)
    lease_expires_at = aware_utc(run.lease_expires_at) if run.lease_expires_at else None
    heartbeat_at = aware_utc(run.heartbeat_at) if run.heartbeat_at else None
    started_at = aware_utc(run.started_at)
    if run.owner_token is None and (lease_expires_at is not None or heartbeat_at is not None):
        return True
    if lease_expires_at is not None:
        return lease_expires_at <= current
    reference = heartbeat_at or started_at
    return reference <= current - fallback_after


def renew_run_lease(
    session: Session,
    model: JobRunModel,
    run_id: int,
    owner_token: str,
    *,
    lease_duration: timedelta = DEFAULT_LEASE_DURATION,
    now: datetime | None = None,
) -> bool:
    renewed_at = aware_utc(now or utcnow())
    result = cast(
        CursorResult[Any],
        session.execute(
            update(model)
            .where(
                model.id == run_id,
                model.owner_token == owner_token,
                model.status == RunStatus.RUNNING.value,
            )
            .values(
                heartbeat_at=renewed_at,
                lease_expires_at=renewed_at + lease_duration,
                lease_version=model.lease_version + 1,
            )
        ),
    )
    return result.rowcount == 1


def finish_owned_run(
    session: Session,
    model: JobRunModel,
    run_id: int,
    owner_token: str,
    values: dict[str, object],
) -> bool:
    """Write terminal fields only while the caller still owns a running row."""

    result = cast(
        CursorResult[Any],
        session.execute(
            update(model)
            .where(
                model.id == run_id,
                model.owner_token == owner_token,
                model.status == RunStatus.RUNNING.value,
                model.completed_at.is_(None),
            )
            .values(**values)
        ),
    )
    return result.rowcount == 1


def update_owned_run(
    session: Session,
    model: JobRunModel,
    run_id: int,
    owner_token: str,
    values: dict[str, object],
) -> bool:
    """Write non-terminal fields only while the caller still owns a running row."""

    result = cast(
        CursorResult[Any],
        session.execute(
            update(model)
            .where(
                model.id == run_id,
                model.owner_token == owner_token,
                model.status == RunStatus.RUNNING.value,
            )
            .values(**values)
        ),
    )
    return result.rowcount == 1


def complete_stale_run(
    session: Session,
    model: JobRunModel,
    run_id: int,
    *,
    owner_token: str | None,
    lease_version: int,
    values: dict[str, object],
) -> bool:
    """Close a stale row without racing a heartbeat observed after selection."""

    result = cast(
        CursorResult[Any],
        session.execute(
            update(model)
            .where(
                model.id == run_id,
                model.owner_token == owner_token,
                model.lease_version == lease_version,
                model.status == RunStatus.RUNNING.value,
                model.completed_at.is_(None),
            )
            .values(**values)
        ),
    )
    return result.rowcount == 1


class RunLeaseHeartbeat:
    """Small daemon writer that keeps a database-owned job lease alive."""

    def __init__(
        self,
        database: Database,
        model: JobRunModel,
        run_id: int,
        owner_token: str,
        *,
        lease_duration: timedelta = DEFAULT_LEASE_DURATION,
        interval: timedelta = HEARTBEAT_INTERVAL,
    ) -> None:
        self.database = database
        self.model = model
        self.run_id = run_id
        self.owner_token = owner_token
        self.lease_duration = lease_duration
        self.interval = interval
        self._stop = threading.Event()
        self._lost = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._loop,
            name=f"runway-job-lease-{self.run_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)
        self._thread = None

    def renew(self) -> bool:
        with self.database.session() as session:
            renewed = renew_run_lease(
                session,
                self.model,
                self.run_id,
                self.owner_token,
                lease_duration=self.lease_duration,
            )
        if not renewed:
            self._lost.set()
        return renewed

    def require(self) -> None:
        if self._lost.is_set() or not self.renew():
            raise LeaseLost(f"run {self.run_id} lease is no longer owned by this worker")

    def _loop(self) -> None:
        while not self._stop.wait(self.interval.total_seconds()):
            try:
                if not self.renew():
                    return
            except Exception:  # noqa: BLE001
                # SQLite can transiently serialize writers. A renewal failure
                # caused by recovery is deterministic and handled above; an I/O
                # failure must not kill an otherwise healthy worker immediately.
                logger.warning(
                    "lease heartbeat renewal failed for run %s", self.run_id, exc_info=True
                )
