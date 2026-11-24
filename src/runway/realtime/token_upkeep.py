"""Instagram long-lived token upkeep (spec 04 section 2).

An Instagram Login long-lived token lasts about 60 days: it has no refresh
token, it can only be refreshed while still valid, and Meta refuses to
refresh one younger than 24 hours. ``TokenUpkeepScheduler`` enqueues a
``refresh_token`` job at about day 50 of the stored token's life; the issue
time is ``extra["issued_at"]``, which every code path that mints a
long-lived token records (tokens written before that field existed derive
it from ``expires_at - 60 days``). The handler refreshes through
``ProviderRegistry.fresh_token`` so the refresh is serialized under the
token lock with every other refresh; a dead grant marks the account
``needs_reauth`` and a transient failure propagates so the queue retries.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from runway.db.base import Database
from runway.db.models import ConnectedAccount, utcnow
from runway.jobs.queue import Job, JobQueue
from runway.jobs.worker import PermanentJobError
from runway.platforms.base import AuthorizationExpired, TokenSet
from runway.platforms.capabilities import Platform
from runway.platforms.registry import ProviderRegistry
from runway.realtime.subscriptions import mark_needs_reauth

logger = logging.getLogger(__name__)

REFRESH_TOKEN_JOB = "refresh_token"
# Meta's long-lived Instagram Login grant lifetime.
INSTAGRAM_TOKEN_LIFETIME = timedelta(days=60)
# "About day 50": late enough that most refreshes still come from the
# opportunistic 30-day sync window, early enough to catch idle accounts.
INSTAGRAM_REFRESH_AT_AGE = timedelta(days=50)
# Meta rejects ig_refresh_token on a token younger than this.
INSTAGRAM_MIN_REFRESH_AGE = timedelta(hours=24)
# The scan itself is hourly; the jobs carry their exact due instant.
SCAN_INTERVAL = timedelta(hours=1)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def instagram_token_issued_at(token: TokenSet) -> datetime | None:
    """When the stored token was issued, or None when it cannot be told.

    ``extra["issued_at"]`` is recorded when a long-lived token is minted;
    for tokens that predate the field the issue time is derived from
    ``expires_at`` minus the documented 60-day lifetime.
    """
    raw = token.extra.get("issued_at")
    if raw:
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            parsed = None
        if parsed is not None:
            return _aware(parsed)
    if token.expires_at is None:
        return None
    return _aware(token.expires_at) - INSTAGRAM_TOKEN_LIFETIME


def instagram_refresh_due(token: TokenSet) -> datetime | None:
    """The instant the token reaches day 50, or None when it cannot be told."""
    issued = instagram_token_issued_at(token)
    return issued + INSTAGRAM_REFRESH_AT_AGE if issued is not None else None


class TokenUpkeepScheduler:
    """Producer of ``refresh_token`` jobs; scans the connected accounts hourly.

    The job for a token's day-50 instant is keyed on that instant, so
    producing again can never duplicate it, and a job enqueued early simply
    waits for its ``run_at``. After a refresh the new token's own day-50 due
    yields a new key; the stale key's job fires later and is a no-op.
    """

    def __init__(
        self,
        database: Database,
        registry: ProviderRegistry,
        queue: JobQueue,
        *,
        interval: timedelta = SCAN_INTERVAL,
    ) -> None:
        self.database = database
        self.registry = registry
        self.queue = queue
        self.interval = interval
        self._last_scan: datetime | None = None

    def next_due(self, now: datetime) -> datetime:
        if self._last_scan is None:
            return now
        return self._last_scan + self.interval

    def produce(self, now: datetime) -> None:
        if self._last_scan is not None and now < self._last_scan + self.interval:
            return
        self._last_scan = now
        with self.database.session() as session:
            rows = session.execute(
                select(
                    ConnectedAccount.id,
                    ConnectedAccount.workspace_id,
                    ConnectedAccount.token_ref,
                ).where(
                    ConnectedAccount.platform == Platform.instagram.value,
                    ConnectedAccount.status == "connected",
                    ConnectedAccount.token_ref.is_not(None),
                )
            ).all()
        for account_id, workspace_id, token_ref in rows:
            token = self.registry.token_store.load(str(token_ref))
            due = instagram_refresh_due(token) if token is not None else None
            if due is None:
                continue
            self.queue.enqueue(
                REFRESH_TOKEN_JOB,
                idempotency_key=(f"{REFRESH_TOKEN_JOB}:{int(account_id)}:{int(due.timestamp())}"),
                run_at=due if due > now else now,
                payload={"account_id": int(account_id)},
                workspace_id=int(workspace_id),
            )


def run_refresh_token_job(
    registry: ProviderRegistry,
    job: Job,
    *,
    clock: Callable[[], datetime] = utcnow,
) -> None:
    """Worker handler for ``refresh_token``: refresh one Instagram grant.

    A missing, disconnected or non-Instagram account is a no-op - the
    connect that queued the job may already have been undone. Meta refuses
    to refresh a token younger than 24 h and a queued job can outlive the
    token it was planned for, so the stored issue time is checked before
    asking. The refresh goes through ``fresh_token`` so it shares the token
    lock with sync's opportunistic refresh; ``AuthorizationExpired`` marks
    the account ``needs_reauth`` while every other provider error
    propagates for the queue's retry.
    """
    account_id = job.payload.get("account_id")
    if not isinstance(account_id, int):
        raise PermanentJobError("refresh_token job needs an account_id")
    with registry.database.session() as session:
        account = session.get(ConnectedAccount, account_id)
        if (
            account is None
            or account.platform != Platform.instagram.value
            or account.status != "connected"
            or not account.token_ref
        ):
            return
        token_ref = str(account.token_ref)
    token = registry.token_store.load(token_ref)
    if token is None:
        mark_needs_reauth(
            registry.database,
            account_id,
            AuthorizationExpired("account token is missing", status=401),
        )
        return
    issued_at = instagram_token_issued_at(token)
    if issued_at is not None and clock() - issued_at < INSTAGRAM_MIN_REFRESH_AGE:
        # A queued job fired while a newer token was just stored.
        return
    try:
        registry.fresh_token(Platform.instagram, token_ref)
    except AuthorizationExpired as exc:
        mark_needs_reauth(registry.database, account_id, exc)
