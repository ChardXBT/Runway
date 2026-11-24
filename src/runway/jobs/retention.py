"""Hourly `purge` job: keeps the database and the lock directories from growing forever.

- idempotency keys after 24 hours and events after 7 days;
- webhook inbox rows after 30 days;
- finished `publish_jobs` after 14 days, `send`/`reconcile` history after 90;
- YouTube-imported rows 30 days old and never refreshed (the platform's
  refresh-or-delete rule, spec 04 section 3);
- every stored platform row of accounts disconnected or revoked for more
  than 7 days, plus their tokens (spec 05 section 5);
- lock files of accounts and token references that no longer exist, when no
  process holds them.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import and_, delete, or_, select, update
from sqlalchemy.orm import Session

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    AccountMetricDay,
    AccountMetricSnapshot,
    CommentRecord,
    ConnectedAccount,
    ContentMetricSnapshot,
    DraftTarget,
    PlatformContent,
    PlatformSubscription,
    PublishJob,
    ReplyDraft,
    WebhookEvent,
    utcnow,
)
from runway.events.log import purge_expired_events
from runway.jobs.queue import RECONCILE, SEND, Job, JobQueue, JobState
from runway.locks import LockTimeout, exclusive_file_lock
from runway.platforms.sync import sync_lock_path
from runway.platforms.tokens import TokenStore, token_store_from_settings
from runway.realtime import standby

logger = logging.getLogger(__name__)

PURGE_JOB = "purge"
PURGE_INTERVAL = timedelta(hours=1)
FINISHED_JOB_RETENTION = timedelta(days=14)
# Sends and reconciliations are the evidence for "did this post go out?".
PUBLISHING_JOB_RETENTION = timedelta(days=90)
# Raw webhook bodies are debugging evidence, not history; 30 days is plenty.
WEBHOOK_EVENT_RETENTION = timedelta(days=30)
# YouTube's authorized-data rule: every stored copy is deleted 30 days after
# a sync last refreshed it (spec 04 section 3).
YOUTUBE_DATA_RETENTION = timedelta(days=30)
# Disconnect revokes at the platform; the stored platform data then dies
# inside the policy window (7 days for YouTube Authorized Data).
DEAD_ACCOUNT_DATA_DELAY = timedelta(days=7)
# `needs_reauth` is what platform-side revocation becomes (failed refresh,
# TikTok authorization.removed, Meta deauthorize) - it takes the same
# deletion path as an explicit disconnect.
DEAD_ACCOUNT_STATES = ("disconnected", "needs_reauth")
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


def purge_webhook_events(session: Session, *, now: datetime | None = None) -> int:
    cutoff = (now or utcnow()) - WEBHOOK_EVENT_RETENTION
    result = session.execute(delete(WebhookEvent).where(WebhookEvent.received_at < cutoff))
    return int(getattr(result, "rowcount", 0) or 0)


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


def _rowcount(result: object) -> int:
    return int(getattr(result, "rowcount", 0) or 0)


def purge_youtube_api_data(session: Session, *, now: datetime) -> int:
    """Delete YouTube-imported rows 30 days old and not refreshed since.

    YouTube's authorized-data policy gives each stored copy 30 days of life,
    renewed by every sync that re-fetches it (spec 04 section 3). Each row
    kind carries the instant it was last refreshed: content ``last_seen_at``
    (inventory) and ``metrics_fetched_at`` (metrics), comments
    ``last_seen_at``, metric days ``fetched_at``. Snapshots are append-only -
    their refresh is a newer snapshot - so age alone retires them, and a
    snapshot dies with the content it belongs to.
    """
    cutoff = now - YOUTUBE_DATA_RETENTION
    removed = 0
    # Comments first: a stale comment dies by its own refresh instant. A
    # reply draft to it can never be sent once the comment is gone, so it
    # dies first - reply_drafts.comment_id has no ON DELETE rule and the
    # delete would otherwise fail on the FK.
    doomed_comments = select(CommentRecord.id).where(
        CommentRecord.platform == "youtube",
        CommentRecord.last_seen_at < cutoff,
    )
    removed += _rowcount(
        session.execute(delete(ReplyDraft).where(ReplyDraft.comment_id.in_(doomed_comments)))
    )
    removed += _rowcount(
        session.execute(delete(CommentRecord).where(CommentRecord.id.in_(doomed_comments)))
    )
    youtube_content = select(PlatformContent.id).where(PlatformContent.platform == "youtube")
    doomed_content = select(PlatformContent.id).where(
        PlatformContent.platform == "youtube",
        PlatformContent.last_seen_at < cutoff,
        or_(
            PlatformContent.metrics_fetched_at.is_(None),
            PlatformContent.metrics_fetched_at < cutoff,
        ),
    )
    removed += _rowcount(
        session.execute(
            delete(ContentMetricSnapshot).where(
                or_(
                    # Orphaned by the content delete below.
                    ContentMetricSnapshot.content_id.in_(doomed_content),
                    and_(
                        ContentMetricSnapshot.captured_at < cutoff,
                        ContentMetricSnapshot.content_id.in_(youtube_content),
                    ),
                )
            )
        )
    )
    youtube_accounts = select(ConnectedAccount.id).where(ConnectedAccount.platform == "youtube")
    removed += _rowcount(
        session.execute(
            delete(AccountMetricDay).where(
                AccountMetricDay.account_id.in_(youtube_accounts),
                AccountMetricDay.fetched_at < cutoff,
            )
        )
    )
    removed += _rowcount(
        session.execute(
            delete(AccountMetricSnapshot).where(
                AccountMetricSnapshot.account_id.in_(youtube_accounts),
                AccountMetricSnapshot.captured_at < cutoff,
            )
        )
    )
    # Draft targets pointing at doomed platform content are Runway-authored
    # rows: they keep their row, their state and their published URL. The
    # published_content_id link is cleared first - it has no ON DELETE rule.
    session.execute(
        update(DraftTarget)
        .where(DraftTarget.published_content_id.in_(doomed_content))
        .values(published_content_id=None)
    )
    removed += _rowcount(
        session.execute(delete(PlatformContent).where(PlatformContent.id.in_(doomed_content)))
    )
    return removed


def purge_dead_account_data(session: Session, token_store: TokenStore, *, now: datetime) -> int:
    """Delete every stored platform row of accounts dead for over 7 days.

    ``disconnected`` is an explicit disconnect; ``needs_reauth`` is a
    platform-side revocation - both take the same deletion path (spec 05
    section 5) once ``updated_at`` is more than 7 days old. A surviving
    token ref is deleted through the token store. The account row itself
    stays (it is the reconnect and audit record), and Runway-authored rows
    - drafts, targets, proposals - are never touched. The webhook inbox
    dies too: an account that no longer exists keeps no deliveries.
    """
    cutoff = now - DEAD_ACCOUNT_DATA_DELAY
    rows = session.execute(
        select(ConnectedAccount.id, ConnectedAccount.token_ref).where(
            ConnectedAccount.status.in_(DEAD_ACCOUNT_STATES),
            ConnectedAccount.updated_at < cutoff,
        )
    ).all()
    account_ids = [int(account_id) for account_id, _ref in rows]
    if not account_ids:
        return 0
    removed = 0
    content_ids = select(PlatformContent.id).where(PlatformContent.account_id.in_(account_ids))
    # Collected before the subscriptions are deleted: a raw delivery whose
    # account was never resolved is reachable only through this link.
    subscription_ids = select(PlatformSubscription.id).where(
        PlatformSubscription.account_id.in_(account_ids)
    )
    removed += _rowcount(
        session.execute(
            delete(ContentMetricSnapshot).where(ContentMetricSnapshot.content_id.in_(content_ids))
        )
    )
    # A reply draft to a deleted comment can never be sent: it dies first
    # (reply_drafts.comment_id has no ON DELETE rule).
    comment_ids = select(CommentRecord.id).where(CommentRecord.account_id.in_(account_ids))
    removed += _rowcount(
        session.execute(delete(ReplyDraft).where(ReplyDraft.comment_id.in_(comment_ids)))
    )
    removed += _rowcount(
        session.execute(delete(CommentRecord).where(CommentRecord.account_id.in_(account_ids)))
    )
    # Draft targets pointing at the account's content keep their row, their
    # state and their published URL; only the link is cleared.
    session.execute(
        update(DraftTarget)
        .where(DraftTarget.published_content_id.in_(content_ids))
        .values(published_content_id=None)
    )
    removed += _rowcount(
        session.execute(delete(PlatformContent).where(PlatformContent.account_id.in_(account_ids)))
    )
    removed += _rowcount(
        session.execute(
            delete(AccountMetricDay).where(AccountMetricDay.account_id.in_(account_ids))
        )
    )
    removed += _rowcount(
        session.execute(
            delete(AccountMetricSnapshot).where(AccountMetricSnapshot.account_id.in_(account_ids))
        )
    )
    # The webhook inbox dies with the account - before the subscriptions
    # go, so subscription-linked deliveries are still found.
    removed += _rowcount(
        session.execute(
            delete(WebhookEvent).where(
                or_(
                    WebhookEvent.account_id.in_(account_ids),
                    WebhookEvent.subscription_id.in_(subscription_ids),
                )
            )
        )
    )
    removed += _rowcount(
        session.execute(
            delete(PlatformSubscription).where(PlatformSubscription.account_id.in_(account_ids))
        )
    )
    for _account_id, token_ref in rows:
        if token_ref:
            token_store.delete(str(token_ref))
    return removed


def run_purge_job(database: Database, settings: Settings, _job: Job) -> None:
    # Spec 07 section 4: a standby machine runs no purge jobs.
    standby.ensure_active(settings)
    # Imported here: runway.api.v1 is an API package, the worker only needs its store.
    from runway.api.v1.idempotency import IdempotencyStore

    token_store = token_store_from_settings(settings, database)
    # Purge is cross-workspace system work by definition.
    with database.system_session() as session:
        duties: list[tuple[str, Callable[[], int]]] = [
            ("idempotency keys", lambda: IdempotencyStore(database).purge_expired(session)),
            ("events", lambda: purge_expired_events(session)),
            ("webhook events", lambda: purge_webhook_events(session)),
            ("finished jobs", lambda: purge_finished_jobs(session)),
            ("stale youtube rows", lambda: purge_youtube_api_data(session, now=utcnow())),
            (
                "dead-account rows",
                lambda: purge_dead_account_data(session, token_store, now=utcnow()),
            ),
        ]
        removed: dict[str, int] = {}
        for name, duty in duties:
            try:
                # One savepoint per duty: a failing duty rolls back its own
                # work only and can never starve the rest of the purge.
                with session.begin_nested():
                    removed[name] = duty()
            except Exception:  # noqa: BLE001
                removed[name] = 0
                logger.exception("retention purge duty %r failed; continuing", name)
        accounts = session.execute(select(ConnectedAccount.id, ConnectedAccount.token_ref)).all()
    token_refs = {str(token_ref) for _id, token_ref in accounts if token_ref}
    sync_names = {sync_lock_path(settings, int(account_id)).stem for account_id, _ in accounts}
    locks = remove_stale_lock_files(token_store.refresh_lock_dir, token_refs)
    locks += remove_stale_lock_files(sync_lock_path(settings, 0).parent, sync_names)
    logger.info(
        "retention purge removed %s idempotency keys, %s events, %s webhook events, "
        "%s jobs, %s stale youtube rows, %s dead-account rows and %s lock files",
        removed["idempotency keys"],
        removed["events"],
        removed["webhook events"],
        removed["finished jobs"],
        removed["stale youtube rows"],
        removed["dead-account rows"],
        locks,
    )
