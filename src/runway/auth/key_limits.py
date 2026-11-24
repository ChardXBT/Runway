"""Per-key limits (spec 06 section 3): request rate and daily caps.

- The request rate is a sliding one-minute window held in memory by each
  process (the API and each `runway mcp` process count separately).
- Daily caps count the domain rows the key created (`content_drafts` for
  posts, `draft_targets.released_*` for releases), read inside the mutation's
  own transaction: enforcement cannot be bypassed by racing the audit write
  or by a create path that writes no audit row. Audit rows remain, for the
  record; they are no longer read for enforcement.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Collection
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from runway.auth.principal import Principal
from runway.auth.problems import Problem, ProblemCode
from runway.db.models import ApiKey, AuditLogEntry, ContentDraft, DraftTarget, utcnow

WINDOW_SECONDS = 60.0
# How long a caller refused on a contended cap lock should wait before retrying.
LOCK_RETRY_AFTER_SECONDS = 5.0


class SlidingWindowLimiter:
    """At most `limit` hits per key in any 60-second window."""

    def __init__(self, limit: int, *, clock: Callable[[], float] = time.monotonic) -> None:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        self.limit = limit
        self.clock = clock
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str) -> float | None:
        """Count one request; return seconds to wait when over the limit, else None."""
        now = self.clock()
        with self._lock:
            window = self._hits.setdefault(key, deque())
            while window and window[0] <= now - WINDOW_SECONDS:
                window.popleft()
            if len(window) >= self.limit:
                return max(window[0] + WINDOW_SECONDS - now, 0.0)
            window.append(now)
            return None


def rate_limited(retry_after: float) -> Problem:
    seconds = max(1, int(retry_after + 0.999))
    return Problem.of(
        ProblemCode.RATE_LIMITED,
        "this API key sent too many requests; wait and retry",
        headers={"Retry-After": str(seconds)},
        extra={"retry_after_seconds": seconds},
    )


def utc_day_start(now: datetime | None = None) -> datetime:
    current = (now or utcnow()).astimezone(UTC)
    return current.replace(hour=0, minute=0, second=0, microsecond=0)


def _count(
    session: Session, principal: Principal, since: datetime, *clauses: ColumnElement[bool]
) -> int:
    statement = (
        select(func.count())
        .select_from(AuditLogEntry)
        .where(
            AuditLogEntry.workspace_id == principal.workspace_id,
            AuditLogEntry.actor_kind == principal.kind,
            AuditLogEntry.actor_id == principal.id,
            AuditLogEntry.created_at >= since,
            *clauses,
        )
    )
    return int(session.scalar(statement) or 0)


def _actor(principal: Principal) -> str:
    """The `created_by` / `released_by` value the engine writes for `principal`."""
    return f"{principal.kind}:{principal.id}"


def acquire_cap_lock(session: Session, principal: Principal) -> None:
    """Serialise the cap check-then-act inside the caller's write transaction.

    Postgres: `SELECT ... FOR UPDATE` on the key's `api_keys` row, so two
    requests by one key queue on it (a keyless agent has no row; the hosted
    profile always uses keys). SQLite: `BEGIN IMMEDIATE` takes the database
    write lock up front; a session that already wrote already holds it, and a
    session that only read upgrades cleanly (WAL). Under contention the call
    waits for `busy_timeout`, then answers `rate_limited` (429) instead of
    letting two creators both pass the cap or surfacing a raw 500.
    """
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        if principal.id.isdigit():
            session.execute(
                select(ApiKey.id).where(ApiKey.id == int(principal.id)).with_for_update()
            )
        return
    if dialect == "sqlite":
        try:
            session.connection().exec_driver_sql("BEGIN IMMEDIATE")
        except OperationalError as exc:
            # A transaction that already wrote is already holding the lock.
            if "cannot start a transaction" in str(exc):
                return
            # The busy_timeout wait expired: the caller can retry once the
            # other write finishes, the same answer a full cap window gives.
            if "database is locked" in str(exc):
                raise rate_limited(LOCK_RETRY_AFTER_SECONDS) from exc
            raise


def posts_created_today(session: Session, principal: Principal, now: datetime | None = None) -> int:
    """Posts this principal created since 00:00 UTC, counted on `content_drafts`."""
    return int(
        session.scalar(
            select(func.count(ContentDraft.id)).where(
                ContentDraft.workspace_id == principal.workspace_id,
                ContentDraft.created_by == _actor(principal),
                ContentDraft.created_at >= utc_day_start(now),
            )
        )
        or 0
    )


def releases_today(
    session: Session, principal: Principal, platform: str, now: datetime | None = None
) -> int:
    """Targets this principal released on `platform` since 00:00 UTC.

    Counted on `draft_targets.released_by` / `released_at`, the release record
    the engine writes inside the release transaction.
    """
    return int(
        session.scalar(
            select(func.count(DraftTarget.id)).where(
                DraftTarget.workspace_id == principal.workspace_id,
                DraftTarget.platform == platform,
                DraftTarget.released_by == _actor(principal),
                DraftTarget.released_at >= utc_day_start(now),
            )
        )
        or 0
    )


def enforce_daily_post_cap(
    session: Session, principal: Principal, cap: int, *, now: datetime | None = None
) -> None:
    """429 `rate_limited` once a key has created `cap` posts today (UTC).

    Users are exempt. Call inside the mutation's own transaction: this locks
    first (`acquire_cap_lock`), then counts that key's posts on the domain
    table, so concurrent creates cannot both pass.
    """
    if principal.kind != "agent":
        return
    acquire_cap_lock(session, principal)
    if posts_created_today(session, principal, now) >= cap:
        raise Problem.of(
            ProblemCode.RATE_LIMITED,
            f"this API key reached its daily cap of {cap} new posts (resets at 00:00 UTC)",
            extra={"daily_cap": cap},
        )


def actions_in(session: Session, principal: Principal, actions: Collection[str]) -> int:
    """Today's audit rows of `principal` with one of `actions` (for tests and reports)."""
    return _count(session, principal, utc_day_start(), AuditLogEntry.action.in_(list(actions)))
