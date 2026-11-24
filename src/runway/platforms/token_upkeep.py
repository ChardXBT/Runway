"""Guaranteed upkeep for Instagram long-lived tokens (spec 08 section 3).

`ProviderRegistry.fresh_token` renews a long-lived Instagram token lazily:
when a sync or publish call finds it inside `INSTAGRAM_TOKEN_REFRESH_WINDOW`
of expiry, it refreshes on the spot. When nothing calls — scheduled sync off,
a quiet account — that never happens and the grant silently dies at day 60.
This producer enqueues a daily `refresh_token` job per due account so the
token is renewed in the second half of its lifetime whether or not anyone
synced.

Meta rules the scheduler honors:

- `ig_refresh_token` refuses tokens younger than 24 hours, so fresh
  exchanges are never enqueued (age comes from `extra["issued_at"]` written
  at exchange/refresh, else estimated from the standard 60-day validity).
- Unified Login tokens are Meta user tokens, not `ig_*` long-lived tokens:
  they are skipped (`extra["login"] == "unified"`).
- A revoked grant (`AuthorizationExpired`) marks the account
  `needs_reauth`; it is never retried in a loop — reconnecting is a person
  action, not a job action.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ConnectedAccount
from runway.jobs.queue import Job, JobQueue
from runway.jobs.worker import PermanentJobError
from runway.platforms.base import AuthorizationExpired, TokenSet
from runway.platforms.capabilities import Platform
from runway.platforms.registry import (
    INSTAGRAM_TOKEN_REFRESH_WINDOW,
    ProviderRegistry,
)

logger = logging.getLogger(__name__)

REFRESH_TOKEN_JOB = "refresh_token"

# Instagram long-lived tokens are issued for ~60 days; without a stored
# issued_at the age is estimated as lifetime minus time-to-live.
_LONG_LIVED_LIFETIME = timedelta(days=60)
# ig_refresh_token refuses tokens younger than this.
_MIN_REFRESH_AGE = timedelta(hours=24)
# The producer tick: due accounts are keyed per day, so producing more often
# changes nothing but the timestamps on skipped rows.
PRODUCE_INTERVAL = timedelta(hours=1)


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _token_age(token: TokenSet, now: datetime) -> timedelta | None:
    """How old the token is, or None when neither record nor estimate exists."""
    issued_text = token.extra.get("issued_at")
    if isinstance(issued_text, str) and issued_text:
        try:
            return now - _aware(datetime.fromisoformat(issued_text.replace("Z", "+00:00")))
        except ValueError:
            pass
    if token.expires_at is None:
        return None
    return now - (_aware(token.expires_at) - _LONG_LIVED_LIFETIME)


def instagram_token_due(token: TokenSet, now: datetime) -> bool:
    """Whether a stored Instagram token should be refreshed now.

    Due means: not a Unified Login token, has expiry information, inside the
    refresh window and old enough for Meta's ig_refresh_token.
    """
    if token.extra.get("login") == "unified":
        return False
    if token.expires_at is None:
        return False
    if _aware(token.expires_at) - now > INSTAGRAM_TOKEN_REFRESH_WINDOW:
        return False
    age = _token_age(token, now)
    return not (age is not None and age < _MIN_REFRESH_AGE)


class TokenUpkeepScheduler:
    """Producer of `refresh_token` jobs; `runway worker` runs them.

    Each due account gets one job per UTC day, keyed
    `refresh_token:<account>:<date>`, so producing again (or from a restarted
    worker) never duplicates the upkeep.
    """

    def __init__(self, database: Database, queue: JobQueue, settings: Settings) -> None:
        self.database = database
        self.queue = queue
        self.registry = ProviderRegistry(database, settings)

    def next_due(self, now: datetime) -> datetime:
        return now + PRODUCE_INTERVAL

    def produce(self, now: datetime) -> None:
        with self.database.session() as session:
            accounts = session.execute(
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
        for account_id, workspace_id, token_ref in accounts:
            token = self.registry.token_store.load(str(token_ref))
            if token is None or not instagram_token_due(token, now):
                continue
            self.queue.enqueue(
                REFRESH_TOKEN_JOB,
                idempotency_key=f"{REFRESH_TOKEN_JOB}:{account_id}:{now.date().isoformat()}",
                run_at=now,
                payload={"account_id": int(account_id), "reason": "long_lived_upkeep"},
                workspace_id=int(workspace_id),
            )


def run_refresh_token_job(
    database: Database,
    settings: Settings,
    job: Job,
    *,
    registry: ProviderRegistry | None = None,
) -> None:
    """Worker handler for `refresh_token`: renews one account's token.

    `ProviderRegistry.fresh_token` does the actual work (refresh lock,
    provider call, re-store). A revoked grant marks the account
    `needs_reauth` and the job ends; anything else propagates so the queue
    retries it.
    """
    account_id = job.payload.get("account_id")
    if not isinstance(account_id, int):
        raise PermanentJobError("refresh_token job has no integer account_id")
    registry = registry or ProviderRegistry(database, settings)
    with database.session() as session:
        account = session.get(ConnectedAccount, account_id)
        if (
            account is None
            or account.platform != Platform.instagram.value
            or account.status != "connected"
            or account.token_ref is None
        ):
            logger.info(
                "token upkeep skipped for account %s: gone or not a connected Instagram account",
                account_id,
            )
            return
        token_ref = str(account.token_ref)
    try:
        registry.fresh_token(Platform.instagram, token_ref)
    except AuthorizationExpired as exc:
        with database.session() as session:
            row = session.get(ConnectedAccount, account_id)
            if row is not None:
                row.status = "needs_reauth"
                row.last_error = exc.detail
        logger.info("account %s token grant is revoked; marked needs_reauth", account_id)
        return
    logger.info("account %s Instagram token renewed by upkeep job", account_id)
