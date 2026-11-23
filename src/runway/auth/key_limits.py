"""Per-key limits (spec 06 section 3): request rate and daily caps.

- The request rate is a sliding one-minute window held in memory by each
  process (the API and each `runway mcp` process count separately).
- Daily caps count audit rows, so they survive restarts and are shared by the
  REST API and the MCP server: every mutation by a key writes one.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable, Collection
from datetime import UTC, datetime

from sqlalchemy import ColumnElement, func, or_, select
from sqlalchemy.orm import Session

from runway.auth.audit import detail_fragment, http_action, mcp_action
from runway.auth.principal import Principal
from runway.auth.problems import Problem, ProblemCode
from runway.db.models import AuditLogEntry, utcnow

WINDOW_SECONDS = 60.0
# Actions that create a post: the v1 route (built by the publishing engine)
# and the MCP tool. Both pass through the same daily cap.
POST_CREATE_ACTIONS = frozenset({http_action("POST", "/api/v1/posts"), mcp_action("create_post")})
# Releases: any v1 route ending in /release, and the MCP tool.
RELEASE_ACTION_PATTERN = "POST /api/v1/%/release"
RELEASE_MCP_ACTION = mcp_action("release_target")


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


def posts_created_today(session: Session, principal: Principal, now: datetime | None = None) -> int:
    return _count(
        session, principal, utc_day_start(now), AuditLogEntry.action.in_(POST_CREATE_ACTIONS)
    )


def releases_today(
    session: Session, principal: Principal, platform: str, now: datetime | None = None
) -> int:
    return _count(
        session,
        principal,
        utc_day_start(now),
        or_(
            AuditLogEntry.action.like(RELEASE_ACTION_PATTERN),
            AuditLogEntry.action == RELEASE_MCP_ACTION,
        ),
        AuditLogEntry.detail_json.like(f"%{detail_fragment('platform', platform)}%"),
    )


def enforce_daily_post_cap(
    session: Session, principal: Principal, cap: int, *, now: datetime | None = None
) -> None:
    """429 `rate_limited` once a key has created `cap` posts today (UTC). Users are exempt."""
    if principal.kind != "agent":
        return
    if posts_created_today(session, principal, now) >= cap:
        raise Problem.of(
            ProblemCode.RATE_LIMITED,
            f"this API key reached its daily cap of {cap} new posts (resets at 00:00 UTC)",
            extra={"daily_cap": cap},
        )


def actions_in(session: Session, principal: Principal, actions: Collection[str]) -> int:
    """Today's audit rows of `principal` with one of `actions` (for tests and reports)."""
    return _count(session, principal, utc_day_start(), AuditLogEntry.action.in_(list(actions)))
