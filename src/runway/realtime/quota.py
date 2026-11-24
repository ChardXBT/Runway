"""Provider quota budgets (spec 04 section 3, RT2 part C).

YouTube's granular 2026 quota gives the project a shared `default` bucket of
10,000 units per Pacific day plus dedicated buckets for `videos.insert`,
`search.list` and `videos.batchGetStats`. The scheduler reserves a share of
`default` for publishing and replies (`youtube_quota_publish_reserve_percent`,
default 30%), divides the rest across the active YouTube accounts, and defers
an account's sync scopes to the next Pacific reset once its share is spent -
`last_error` reads "quota budget reached" and one `quota.low` event per day
and account is emitted. Publishing calls are never blocked by the budget.

Instagram reports its pressure in the `X-App-Usage` and
`X-Business-Use-Case-Usage` response headers; at 80% or more the account's
sync scopes defer by one hour.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    ConnectedAccount,
    EventRecord,
    QuotaUsage,
    SyncRun,
    utcnow,
)
from runway.events.log import record_event
from runway.events.types import QUOTA_LOW
from runway.realtime import schedule

# YouTube's quota resets at midnight Pacific (spec 04 section 3; research
# youtube.md section 1).
PACIFIC_TZ = ZoneInfo("America/Los_Angeles")

logger = logging.getLogger(__name__)

QUOTA_BUDGET_ERROR = "quota budget reached"
INSTAGRAM_USAGE_THRESHOLD = 80.0
INSTAGRAM_USAGE_DEFER = timedelta(hours=1)

# YouTube API method -> (quota bucket, units per call).
# Costs read 2026-11-23 from the quota-cost table rechecked in
# `runway-orchestration/docs/expansion/research/recheck-20261123.md` section 3
# and `docs/expansion/research/youtube.md` section 1. The three own-bucket
# methods charge their listed unit cost into their dedicated bucket; the
# Analytics API `reports` call is not a Data API method but is charged 1 unit
# against `default` so run estimates and the recorded ledger agree.
YOUTUBE_METHOD_COSTS: dict[str, tuple[str, int]] = {
    "videos.list": ("default", 1),
    "channels.list": ("default", 1),
    "playlistItems.list": ("default", 1),
    "commentThreads.list": ("default", 1),
    "comments.list": ("default", 1),
    "comments.insert": ("default", 50),
    "videos.insert": ("videos.insert", 100),
    "thumbnails.set": ("default", 50),
    "search.list": ("search.list", 100),
    "videos.batchGetStats": ("videos.batchGetStats", 1),
    "analytics.reports": ("default", 1),
}

# Daily cap per bucket, expressed in the table's units. `videos.insert` and
# `search.list` cap at 100 calls (100 calls x their unit cost);
# `videos.batchGetStats` caps at 10,000 calls of 1 unit.
YOUTUBE_BUCKET_DAILY: dict[str, int] = {
    "default": 10_000,
    "videos.insert": 10_000,
    "search.list": 10_000,
    "videos.batchGetStats": 10_000,
}


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _sessions(source: Session | Database) -> AbstractContextManager[Session]:
    if isinstance(source, Database):
        return source.session()
    return nullcontext(source)


def pacific_day(now: datetime) -> date:
    """The quota day containing `now`: the Los Angeles calendar date."""
    aware = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return aware.astimezone(PACIFIC_TZ).date()


def _pacific_day_start(day: date) -> datetime:
    """The UTC instant of the Pacific midnight opening `day`."""
    return datetime.combine(day, time.min, tzinfo=PACIFIC_TZ).astimezone(UTC)


def pacific_day_start(now: datetime) -> datetime:
    return _pacific_day_start(pacific_day(now))


def next_pacific_reset(now: datetime) -> datetime:
    """The UTC instant of the next Pacific midnight (the quota reset)."""
    return _pacific_day_start(pacific_day(now) + timedelta(days=1))


# ------------------------------------------------------------- unit recording


def _upsert_insert(dialect: str) -> Callable[..., Any] | None:
    """The dialect's upsert-capable insert, or None where it does not exist."""
    if dialect == "sqlite":
        return sqlite_insert
    if dialect == "postgresql":
        return postgresql_insert
    return None


def record_units(
    session: Session,
    provider: str,
    bucket: str,
    units: int,
    *,
    day: date,
) -> QuotaUsage:
    """Add `units` to the (provider, bucket, day) row; the caller commits.

    The insert-or-add is one atomic dialect upsert: two sessions recording
    the same key can never duplicate-insert on `uq_quota_usage_day` - the
    loser adds its units onto the committed row.
    """
    moment = utcnow()
    upsert = _upsert_insert(session.get_bind().dialect.name)
    if upsert is not None:
        statement = upsert(QuotaUsage).values(
            provider=provider,
            bucket=bucket,
            day=day,
            units_used=units,
            updated_at=moment,
        )
        statement = statement.on_conflict_do_update(
            index_elements=["provider", "bucket", "day"],
            set_={
                "units_used": QuotaUsage.units_used + statement.excluded.units_used,
                "updated_at": moment,
            },
        )
        return session.execute(statement.returning(QuotaUsage)).scalar_one()
    # Dialects without an upsert keep the read-modify-write form.
    row = session.scalar(
        select(QuotaUsage).where(
            QuotaUsage.provider == provider,
            QuotaUsage.bucket == bucket,
            QuotaUsage.day == day,
        )
    )
    if row is None:
        row = QuotaUsage(provider=provider, bucket=bucket, day=day, units_used=0)
        session.add(row)
        session.flush()
    row.units_used += units
    row.updated_at = moment
    return row


def record_youtube_call(
    session: Session, method: str, *, now: datetime | None = None
) -> tuple[str, int]:
    """Charge one YouTube API call to its bucket on the Pacific-day row."""
    bucket, units = YOUTUBE_METHOD_COSTS.get(method, ("default", 1))
    record_units(session, "youtube", bucket, units, day=pacific_day(now or utcnow()))
    return bucket, units


def used_units(source: Session | Database, provider: str, bucket: str, *, day: date) -> int:
    """Units recorded for (provider, bucket) on `day`."""
    with _sessions(source) as session:
        return int(
            session.scalar(
                select(func.coalesce(func.sum(QuotaUsage.units_used), 0)).where(
                    QuotaUsage.provider == provider,
                    QuotaUsage.bucket == bucket,
                    QuotaUsage.day == day,
                )
            )
            or 0
        )


class QuotaRecorder:
    """Provider-side seam: persists each API call's units in its own commit.

    Attached to the provider by `ProviderRegistry` (`quota_recorder`); every
    call the provider makes lands on the project's (provider, bucket,
    Pacific-day) row whether it ran inside a sync or not.
    """

    def __init__(self, database: Database, provider: str = "youtube") -> None:
        self.database = database
        self.provider = provider

    def __call__(self, method: str, bucket: str, units: int) -> None:  # noqa: ARG002
        try:
            with self.database.session() as session:
                record_units(session, self.provider, bucket, units, day=pacific_day(utcnow()))
        except Exception:  # noqa: BLE001
            # Bookkeeping rides on a call that already happened; a ledger
            # failure must never propagate into the provider and make a
            # successful platform call look failed.
            logger.warning(
                "could not record %s quota units for %s %s",
                units,
                self.provider,
                bucket,
                exc_info=True,
            )


# ---------------------------------------------------------------- the budget


def sync_budget_units(settings: Settings) -> int:
    """Units of the `default` bucket sync may spend per Pacific day.

    The reserve percent is held back for publishing and replies, which are
    never blocked by this budget.
    """
    total = YOUTUBE_BUCKET_DAILY["default"]
    reserve = total * settings.youtube_quota_publish_reserve_percent // 100
    return total - reserve


def youtube_account_share(source: Session | Database, settings: Settings) -> float:
    """Each active YouTube account's share of the sync budget."""
    with _sessions(source) as session:
        accounts = int(
            session.scalar(
                select(func.count())
                .select_from(ConnectedAccount)
                .where(
                    ConnectedAccount.platform == "youtube",
                    ConnectedAccount.status == "connected",
                )
            )
            or 0
        )
    return sync_budget_units(settings) / max(accounts, 1)


def account_sync_spend(source: Session | Database, account_id: int, *, now: datetime) -> int:
    """Units the account's sync runs already consumed on this Pacific day."""
    day_start = pacific_day_start(now)
    with _sessions(source) as session:
        return int(
            session.scalar(
                select(func.coalesce(func.sum(SyncRun.quota_units_estimate), 0)).where(
                    SyncRun.account_id == account_id,
                    SyncRun.started_at >= day_start,
                )
            )
            or 0
        )


def youtube_sync_share_spent(
    source: Session | Database, settings: Settings, account_id: int, *, now: datetime
) -> bool:
    """True once the account's sync runs spent its share of the budget."""
    share = youtube_account_share(source, settings)
    if share <= 0:
        return True
    return account_sync_spend(source, account_id, now=now) >= share


def provider_quota_summary(
    source: Session | Database, provider: str, settings: Settings, *, now: datetime | None = None
) -> dict[str, int | None]:
    """Today's quota view for the sync-status route.

    `units_used` is the provider's `default` bucket spend on the current
    Pacific day. `budget`/`reserve` exist only for YouTube, the one provider
    with a unit budget; other providers report them as null.
    """
    day = pacific_day(now or utcnow())
    used = used_units(source, provider, "default", day=day)
    if provider == "youtube":
        total = YOUTUBE_BUCKET_DAILY["default"]
        reserve = total * settings.youtube_quota_publish_reserve_percent // 100
        return {"units_used": used, "budget": total, "reserve": reserve}
    return {"units_used": used, "budget": None, "reserve": None}


def _quota_low_emitted_today(session: Session, account: ConnectedAccount, now: datetime) -> bool:
    return (
        session.scalar(
            select(func.count())
            .select_from(EventRecord)
            .where(
                EventRecord.type == QUOTA_LOW,
                EventRecord.subject_type == "account",
                EventRecord.subject_id == str(account.id),
                EventRecord.created_at >= pacific_day_start(now),
            )
        )
        or 0
    ) > 0


def enforce_youtube_budget(
    source: Session | Database,
    account_id: int,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> bool:
    """Defer the account's scopes to the next Pacific reset when its share is
    spent; emits one `quota.low` event per account per Pacific day.

    Returns True when the account is over budget (deferred), False otherwise.
    Inside `AdaptiveSyncScheduler.produce` a True result skips the enqueue so
    the budget degrades intervals instead of failing syncs.
    """
    moment = now or utcnow()
    with _sessions(source) as session:
        account = session.get(ConnectedAccount, account_id)
        if account is None or account.platform != "youtube":
            return False
        if not youtube_sync_share_spent(session, settings, account_id, now=moment):
            return False
        reset = next_pacific_reset(moment)
        schedule.defer_account_scopes(
            session,
            account_id,
            reset,
            now=moment,
            reason=QUOTA_BUDGET_ERROR,
        )
        if not _quota_low_emitted_today(session, account, moment):
            record_event(
                session,
                int(account.workspace_id),
                QUOTA_LOW,
                "account",
                int(account.id),
                {
                    "provider": "youtube",
                    "bucket": "default",
                    "reason": QUOTA_BUDGET_ERROR,
                    "deferred_until": reset.isoformat(),
                },
            )
        return True


def enforce_instagram_usage(
    source: Session | Database,
    account_id: int,
    usage: dict[str, float],
    *,
    now: datetime | None = None,
) -> bool:
    """Defer the account's scopes by an hour when a usage header is >= 80%."""
    moment = now or utcnow()
    if not usage or max(usage.values()) < INSTAGRAM_USAGE_THRESHOLD:
        return False
    with _sessions(source) as session:
        account = session.get(ConnectedAccount, account_id)
        if account is None:
            return False
        schedule.defer_account_scopes(
            session,
            account_id,
            moment + INSTAGRAM_USAGE_DEFER,
            now=moment,
        )
        return True


# The recorder type providers store: (method, bucket, units) -> None.
QuotaCallRecorder = Callable[[str, str, int], None]
