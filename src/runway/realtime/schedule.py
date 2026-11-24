"""Adaptive per-scope sync scheduling (spec 04 section 3, RT2 part B).

The fixed-interval `PlatformSyncScheduler` synced every scope of every
connected account in one run. The adaptive loop gives each of the six scopes
its own cadence instead, recorded on `account_sync_state`:

* `next_run_at` says when the scope is due; `produce` turns a due scope into
  a `sync_account` job carrying `{"account_id", "scope", "trigger": "scheduled"}`
  keyed `sync_account:<account>:<scope>:<due slot>` so producing again (or a
  restarted worker) can never duplicate it.
* `boosted_until` opens a fast-cadence window - a publish or a push hint
  makes `inventory` run every 2 minutes for 30 minutes, a publish makes
  `content_metrics_recent` run every 5 minutes for 2 hours, and the inbox
  page boosts `comments` while it is open. `boost()` below is the public
  seam: RT1's webhook processing calls it on a hint, the publishing engine
  calls it after a publish, and the inbox route calls it while the page is
  open for that account.
* `consecutive_failures` drives error backoff: normal interval x 2^failures
  capped at 24 h with +/-20% jitter; a provider's `RateLimited` retry-after
  wins when present.
* Fourteen days without a request from a human principal (recorded on
  `workspaces.last_seen_at` by `get_principal`) drops every scope to at most
  once per 24 h.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import AccountSyncState, ConnectedAccount, Workspace, utcnow
from runway.jobs.queue import JobQueue

logger = logging.getLogger(__name__)

# The six scopes, in produce order. `run_sync_account_job` rejects payloads
# whose `scope` is not one of these.
SCOPES: tuple[str, ...] = (
    "identity",
    "inventory",
    "content_metrics_recent",
    "content_metrics_older",
    "comments",
    "daily_metrics",
)

# scope -> (normal interval, boosted interval or None)
# Spec 04 section 3: identity 24 h / on reconnect; inventory 30 min / 2 min
# for 30 min after a publish or push hint; content_metrics_recent 30 min /
# 5 min for 2 h after a publish; content_metrics_older 24 h; comments
# 15 min / 2 min while boosted by the inbox page; daily_metrics 6 h.
_SCOPE_INTERVALS: dict[str, tuple[timedelta, timedelta | None]] = {
    "identity": (timedelta(hours=24), None),
    "inventory": (timedelta(minutes=30), timedelta(minutes=2)),
    "content_metrics_recent": (timedelta(minutes=30), timedelta(minutes=5)),
    "content_metrics_older": (timedelta(hours=24), None),
    "comments": (timedelta(minutes=15), timedelta(minutes=2)),
    "daily_metrics": (timedelta(hours=6), None),
}

# How long `inventory` stays boosted after a publish or push hint, and
# `content_metrics_recent` after a publish (spec 04 section 3).
INVENTORY_BOOST_MINUTES = 30
RECENT_METRICS_BOOST_MINUTES = 120

INACTIVITY_THRESHOLD = timedelta(days=14)
INACTIVITY_FLOOR = timedelta(hours=24)
BACKOFF_CAP = timedelta(hours=24)
BACKOFF_JITTER = 0.2
# A skipped scope (CapabilityUnsupported) retries tomorrow, not on the
# scope's normal cadence - the capability is not about to appear.
SKIPPED_RESCHEDULE = timedelta(hours=24)


def _aware(moment: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes for timezone-aware columns; compare aware."""
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def normal_interval(scope: str) -> timedelta:
    return _SCOPE_INTERVALS[scope][0]


def is_boosted(state: AccountSyncState, now: datetime) -> bool:
    boosted_until = _aware(state.boosted_until)
    return boosted_until is not None and boosted_until > now


def workspace_inactive(last_seen_at: datetime | None, now: datetime) -> bool:
    seen = _aware(last_seen_at)
    return seen is None or now - seen >= INACTIVITY_THRESHOLD


def effective_interval(
    scope: str,
    boosted_until: datetime | None,
    last_seen_at: datetime | None,
    now: datetime,
) -> timedelta:
    """The cadence a scope is on right now, including the inactivity floor."""
    normal, boosted = _SCOPE_INTERVALS[scope]
    interval = normal
    boost_until = _aware(boosted_until)
    if boosted is not None and boost_until is not None and boost_until > now:
        interval = boosted
    if workspace_inactive(last_seen_at, now):
        interval = max(interval, INACTIVITY_FLOOR)
    return interval


def failure_delay(
    normal: timedelta,
    consecutive_failures: int,
    *,
    retry_after: float | None = None,
    rand: Callable[[], float] = random.random,
) -> timedelta:
    """normal x 2^failures capped at 24 h, jittered +/-20%; retry-after wins."""
    if retry_after is not None:
        return timedelta(seconds=retry_after)
    # `1 << n` stays `int` where `2**n` is `Any` to mypy; clamping n keeps the
    # shift legal and the product inside timedelta's range before `min` caps.
    base = min(normal * (1 << min(max(consecutive_failures, 0), 28)), BACKOFF_CAP)
    spread = base * BACKOFF_JITTER * (rand() * 2 - 1)
    return base + spread


def _sessions(source: Session | Database) -> AbstractContextManager[Session]:
    if isinstance(source, Database):
        return source.session()
    return nullcontext(source)


def _state_row(
    session: Session, account: ConnectedAccount, scope: str, now: datetime
) -> AccountSyncState:
    state = session.scalar(
        select(AccountSyncState).where(
            AccountSyncState.account_id == account.id,
            AccountSyncState.scope == scope,
        )
    )
    if state is None:
        state = AccountSyncState(
            workspace_id=int(account.workspace_id),
            account_id=int(account.id),
            scope=scope,
            next_run_at=now,
            source="poll",
            consecutive_failures=0,
            updated_at=now,
        )
        session.add(state)
        session.flush()
    return state


def ensure_states(
    session: Session, account: ConnectedAccount, now: datetime
) -> dict[str, AccountSyncState]:
    """One state row per scope, created due-now when missing."""
    return {scope: _state_row(session, account, scope, now) for scope in SCOPES}


def boost(
    session_or_db: Session | Database,
    account_id: int,
    scope: str,
    minutes: float,
    *,
    now: datetime | None = None,
) -> None:
    """Boost a scope: `boosted_until = now + minutes`, `source = "push"`.

    Pulls `next_run_at` forward to now so the next produce enqueues the scope
    within its boosted interval. Called from RT1's webhook processing (push
    hint -> inventory), the publishing engine (publish -> inventory and
    content_metrics_recent) and the inbox page (open -> comments).
    """
    if scope not in SCOPES:
        raise ValueError(f"unknown sync scope {scope!r}")
    moment = now or utcnow()
    with _sessions(session_or_db) as session:
        account = session.get(ConnectedAccount, account_id)
        if account is None:
            return
        state = _state_row(session, account, scope, moment)
        boosted_until = _aware(state.boosted_until)
        state.boosted_until = max(boosted_until or moment, moment + timedelta(minutes=minutes))
        next_run = _aware(state.next_run_at)
        state.next_run_at = min(next_run or moment, moment)
        state.source = "push"
        state.updated_at = moment


def defer_account_scopes(
    source: Session | Database,
    account_id: int,
    until: datetime,
    *,
    reason: str | None = None,
    now: datetime | None = None,
) -> None:
    """Push every scope's `next_run_at` out to at least `until` (never earlier).

    Used by the quota budget (next Pacific reset, `reason` "quota budget
    reached") and Instagram's usage-header backpressure (+1 h, no reason -
    throttling is not an error). Scopes that have never been scheduled get a
    state row already pushed out.
    """
    moment = now or utcnow()
    with _sessions(source) as session:
        account = session.get(ConnectedAccount, account_id)
        if account is None:
            return
        for scope in SCOPES:
            state = _state_row(session, account, scope, moment)
            next_run = _aware(state.next_run_at)
            if next_run is None or next_run < until:
                state.next_run_at = until
            if reason is not None:
                state.last_error = reason
            state.updated_at = moment


def record_scope_run(
    source: Session | Database,
    account_id: int,
    scope: str,
    *,
    status: str,
    error: str | None = None,
    retry_after: float | None = None,
    now: datetime | None = None,
    rand: Callable[[], float] = random.random,
) -> None:
    """Record one scope outcome and schedule its next run.

    A failure backs off exponentially (see `failure_delay`); a `skipped`
    outcome (CapabilityUnsupported) waits a day - polling a capability the
    provider does not have on the normal cadence is wasted quota; anything
    else resumes the effective cadence. `source` stays `push` while the
    boost window the last run served is still open, else `poll`. Pass a
    `Session` to write inside the caller's transaction (spec 04 section 4
    events).
    """
    if scope not in SCOPES:
        raise ValueError(f"unknown sync scope {scope!r}")
    moment = now or utcnow()
    with _sessions(source) as session:
        account = session.get(ConnectedAccount, account_id)
        if account is None:
            return
        state = _state_row(session, account, scope, moment)
        state.last_run_at = moment
        state.last_status = status
        state.last_error = error
        if status == "failed":
            state.consecutive_failures += 1
            delay = failure_delay(
                normal_interval(scope),
                state.consecutive_failures,
                retry_after=retry_after,
                rand=rand,
            )
            workspace = session.get(Workspace, int(account.workspace_id))
            if workspace_inactive(workspace.last_seen_at if workspace else None, moment):
                delay = max(delay, INACTIVITY_FLOOR)
        elif status == "skipped":
            state.consecutive_failures = 0
            delay = SKIPPED_RESCHEDULE
        else:
            state.consecutive_failures = 0
            workspace = session.get(Workspace, int(account.workspace_id))
            delay = effective_interval(
                scope,
                state.boosted_until,
                workspace.last_seen_at if workspace else None,
                moment,
            )
        state.next_run_at = moment + delay
        state.source = "push" if is_boosted(state, moment) else "poll"
        state.updated_at = moment


class AdaptiveSyncScheduler:
    """Producer of scoped `sync_account` jobs; `runway worker` runs them.

    Replaces `PlatformSyncScheduler` when `adaptive_sync_enabled` is set. For
    each connected account and scope whose `next_run_at` is due it enqueues a
    job keyed on the due slot, then advances the scope's next run by its
    effective interval.
    """

    def __init__(
        self,
        database: Database,
        queue: JobQueue,
        settings: Settings,
        *,
        job_kind: str = "sync_account",
    ) -> None:
        self.database = database
        self.queue = queue
        self.settings = settings
        self.job_kind = job_kind

    def next_due(self, now: datetime) -> datetime | None:
        # Producer sweep: scans every workspace's sync schedule.
        with self.database.system_session() as session:
            account_ids = session.scalars(
                select(ConnectedAccount.id).where(ConnectedAccount.status == "connected")
            ).all()
            if not account_ids:
                return None
            rows = session.execute(
                select(AccountSyncState.account_id, AccountSyncState.next_run_at).where(
                    AccountSyncState.account_id.in_(account_ids)
                )
            ).all()
        covered = {account_id for account_id, _ in rows}
        if covered != set(int(a) for a in account_ids):
            return now  # an account is missing every state row: bootstrap it
        due_times = [_aware(next_run) or now for _, next_run in rows]
        return min(due_times) if due_times else now

    def produce(self, now: datetime) -> None:
        # Producer sweep: scans every workspace's sync schedule.
        with self.database.system_session() as session:
            accounts = session.scalars(
                select(ConnectedAccount).where(ConnectedAccount.status == "connected")
            ).all()
            workspaces = {
                int(workspace.id): workspace
                for workspace in session.scalars(select(Workspace)).all()
            }
            from runway.realtime import quota  # local: quota imports this module

            for account in accounts:
                if account.platform == "youtube" and quota.enforce_youtube_budget(
                    session, int(account.id), self.settings, now=now
                ):
                    # Over its share of the default bucket: scopes sit out
                    # until the Pacific reset (spec 04 section 3).
                    continue
                states = ensure_states(session, account, now)
                last_seen = (
                    workspaces[int(account.workspace_id)].last_seen_at
                    if int(account.workspace_id) in workspaces
                    else None
                )
                for scope, state in states.items():
                    next_run = _aware(state.next_run_at)
                    if next_run is not None and next_run > now:
                        continue
                    # `next_run` is the due slot this job belongs to; state
                    # rows always carry one (a null would be a writer bug,
                    # surfaced loudly rather than re-keyed onto now).
                    assert next_run is not None
                    slot = int(next_run.timestamp())
                    self.queue.enqueue_pending_job(
                        session,
                        self.job_kind,
                        idempotency_key=f"{self.job_kind}:{account.id}:{scope}:{slot}",
                        run_at=now,
                        payload={
                            "account_id": int(account.id),
                            "scope": scope,
                            "trigger": "scheduled",
                        },
                        workspace_id=int(account.workspace_id),
                        # A missed slot is covered by the next due time; do not
                        # pile up retries.
                        max_attempts=1,
                    )
                    state.next_run_at = now + effective_interval(
                        scope, state.boosted_until, last_seen, now
                    )
                    state.updated_at = now
