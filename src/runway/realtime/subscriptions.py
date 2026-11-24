"""Push subscription upkeep: YouTube WebSub subscribe, renew and unsubscribe.

Spec 04 section 2: on a YouTube account connect - and only when a public
callback base exists (``public_edge_url`` on a local install, the public
OAuth redirect base on hosted) - Runway creates a ``platform_subscriptions``
row for the channel's topic and enqueues a ``renew_subscription`` job. The
job POSTs the WebSub form to Google's hub through an injectable HTTP client;
the hub then calls the RT1 GET verification, which records the granted lease
and flips the row to ``active``.

A producer enqueues ``renew_subscription`` when
``now >= subscribed_at + 0.8 * lease_seconds`` - Google's hub grants about
five days (432,000 s) whatever is requested and stops silently at expiry, so
renewing early keeps the push channel alive (research recheck-20261122
section 1). A subscription whose lease lapses becomes ``expired`` and is
resubscribed. On disconnect the row is marked ``unsubscribed``, any pending
subscribe job is cancelled, and an unsubscribe job posts ``hub.mode=
unsubscribe`` so the hub stops delivering and frees the lease.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    ConnectedAccount,
    PlatformSubscription,
    PublishJob,
    utcnow,
)
from runway.jobs.queue import JOB_STATE_MACHINE, Job, JobQueue, JobState
from runway.jobs.worker import PermanentJobError
from runway.platforms.base import ProviderError, safe_error_summary

logger = logging.getLogger(__name__)

RENEW_SUBSCRIPTION_JOB = "renew_subscription"
WEBSUB_HUB_URL = "https://pubsubhubbub.appspot.com/subscribe"
# Google's hub grants about five days whatever is asked (recheck-20261122
# section 1); ask for it explicitly and renew once 80% has passed.
WEBSUB_LEASE_SECONDS = 432_000
RENEW_AT_FRACTION = 0.8

# A WebSub form POST returning an HTTP status; injectable so tests never
# touch the network.
HttpPost = Callable[[str, dict[str, str]], int]


def youtube_topic(channel_id: str) -> str:
    return f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={channel_id}"


def _callback_base(settings: Settings) -> str | None:
    """The public base the hub can reach, or None when there is none.

    Local installs publish only through the configured edge (spec 04 section
    6). Hosted serves the API publicly; its OAuth redirect base is the public
    base the callbacks share.
    """
    if settings.public_edge_url:
        return settings.public_edge_url.rstrip("/")
    if settings.profile == "hosted" and settings.oauth_redirect_base_url:
        return settings.resolved_oauth_redirect_base_url
    return None


def _post_form(url: str, fields: dict[str, str]) -> int:
    """The default hub client: one POST of the WebSub form, status returned."""
    try:
        with httpx.Client(timeout=20.0) as client:
            return client.post(url, data=fields).status_code
    except httpx.HTTPError as exc:
        raise ProviderError(
            f"WebSub hub request failed: {safe_error_summary(exc)}",
            retryable=True,
        ) from exc


def on_account_connected(
    session: Session,
    queue: JobQueue,
    settings: Settings,
    account: ConnectedAccount,
) -> PlatformSubscription | None:
    """Subscription side of an account connect, inside the caller's transaction.

    YouTube gets a ``platform_subscriptions`` row (fresh random ``hub_secret``
    and ``callback_token`` per connect, so a stale callback URL never
    authenticates) plus the ``renew_subscription`` job that posts the
    subscribe form. Without a public callback base nothing is created - the
    install simply polls (spec 04 section 6).
    """
    if account.platform != "youtube":
        return None
    if _callback_base(settings) is None:
        return None
    topic = youtube_topic(str(account.external_account_id))
    subscription = session.scalar(
        select(PlatformSubscription).where(
            PlatformSubscription.account_id == account.id,
            PlatformSubscription.provider == "youtube",
            PlatformSubscription.topic == topic,
        )
    )
    if subscription is None:
        subscription = PlatformSubscription(
            account_id=int(account.id),
            provider="youtube",
            topic=topic,
        )
        session.add(subscription)
    subscription.hub_secret = secrets.token_hex(32)
    subscription.callback_token = secrets.token_hex(32)
    subscription.state = "pending"
    subscription.lease_seconds = None
    subscription.subscribed_at = None
    subscription.expires_at = None
    subscription.last_error = None
    session.flush()
    # The callback token rotates per connect, so keying the subscribe job on
    # it gives exactly one job per connect even when a past job kept its key.
    queue.enqueue_pending_job(
        session,
        RENEW_SUBSCRIPTION_JOB,
        idempotency_key=(
            f"{RENEW_SUBSCRIPTION_JOB}:{subscription.id}:connect:{subscription.callback_token}"
        ),
        run_at=utcnow(),
        payload={"subscription_id": int(subscription.id), "mode": "subscribe"},
        workspace_id=int(account.workspace_id),
    )
    return subscription


def on_account_disconnected(session: Session, queue: JobQueue, account: ConnectedAccount) -> None:
    """Stop every live YouTube push channel of the account, in the caller's transaction.

    Pending subscribe jobs are cancelled first so a queued subscribe cannot
    resurrect a channel the owner just disconnected; one ``unsubscribe`` job
    per subscription then tells the hub to stop. The row is marked
    ``unsubscribed`` up front: if the hub still calls the verify GET after
    this, the route's state check refuses it (only ``pending``/``active`` may
    subscribe).
    """
    subscriptions = session.scalars(
        select(PlatformSubscription).where(
            PlatformSubscription.account_id == account.id,
            PlatformSubscription.provider == "youtube",
            PlatformSubscription.state != "unsubscribed",
        )
    ).all()
    if not subscriptions:
        return
    subscription_ids = {int(subscription.id) for subscription in subscriptions}
    pending = session.scalars(
        select(PublishJob).where(
            PublishJob.kind == RENEW_SUBSCRIPTION_JOB,
            PublishJob.state == JobState.PENDING.value,
        )
    ).all()
    for row in pending:
        try:
            payload = json.loads(row.payload_json or "{}")
        except json.JSONDecodeError:
            continue
        if (
            isinstance(payload, dict)
            and payload.get("subscription_id") in subscription_ids
            and payload.get("mode") == "subscribe"
        ):
            JOB_STATE_MACHINE.apply(row, JobState.CANCELLED)
    moment = utcnow()
    for subscription in subscriptions:
        subscription.state = "unsubscribed"
        subscription.expires_at = moment
        queue.enqueue_pending_job(
            session,
            RENEW_SUBSCRIPTION_JOB,
            # The callback token rotates per connect, so this key is unique
            # per disconnect cycle.
            idempotency_key=(
                f"{RENEW_SUBSCRIPTION_JOB}:{subscription.id}"
                f":unsubscribe:{subscription.callback_token}"
            ),
            run_at=moment,
            payload={"subscription_id": int(subscription.id), "mode": "unsubscribe"},
            workspace_id=int(subscription.workspace_id),
        )


def _aware(moment: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes for aware columns; compare aware."""
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def renewal_due(subscription: PlatformSubscription) -> datetime | None:
    """The instant the lease reaches 80%: ``subscribed_at + 0.8 * lease_seconds``."""
    subscribed_at = _aware(subscription.subscribed_at)
    if subscribed_at is None or subscription.lease_seconds is None:
        return None
    return subscribed_at + timedelta(seconds=subscription.lease_seconds * RENEW_AT_FRACTION)


class SubscriptionScheduler:
    """Producer of ``renew_subscription`` jobs; ``runway worker`` runs them.

    An ``active`` subscription is renewed the moment 80% of its granted lease
    has passed - the job is keyed on that due instant, so producing again can
    never duplicate it, and a lease that already lapsed flips the row to
    ``expired`` with a resubscribe job keyed on the expiry instant.
    """

    def __init__(self, database: Database, queue: JobQueue) -> None:
        self.database = database
        self.queue = queue

    def _enqueue(
        self,
        session: Session,
        subscription: PlatformSubscription,
        *,
        key_slot: int,
        now: datetime,
    ) -> None:
        self.queue.enqueue_pending_job(
            session,
            RENEW_SUBSCRIPTION_JOB,
            idempotency_key=f"{RENEW_SUBSCRIPTION_JOB}:{subscription.id}:due:{key_slot}",
            run_at=now,
            payload={"subscription_id": int(subscription.id), "mode": "subscribe"},
            workspace_id=int(subscription.workspace_id),
        )

    def next_due(self, now: datetime) -> datetime | None:
        with self.database.session() as session:
            subscriptions = session.scalars(
                select(PlatformSubscription).where(PlatformSubscription.state == "active")
            ).all()
        upcoming: list[datetime] = []
        for subscription in subscriptions:
            expires_at = _aware(subscription.expires_at)
            if expires_at is not None:
                upcoming.append(expires_at)
            due = renewal_due(subscription)
            if due is not None:
                upcoming.append(due)
        return min(upcoming) if upcoming else None

    def produce(self, now: datetime) -> None:
        with self.database.session() as session:
            subscriptions = session.scalars(select(PlatformSubscription)).all()
            for subscription in subscriptions:
                expires_at = _aware(subscription.expires_at)
                if subscription.state == "active" and expires_at is not None and expires_at <= now:
                    # The lease lapsed unrenewed: mark it, then resubscribe.
                    subscription.state = "expired"
                    self._enqueue(
                        session,
                        subscription,
                        key_slot=int(expires_at.timestamp()),
                        now=now,
                    )
                    continue
                if subscription.state != "active":
                    continue
                due = renewal_due(subscription)
                if due is None or now < due:
                    continue
                self._enqueue(
                    session,
                    subscription,
                    key_slot=int(due.timestamp()),
                    now=now,
                )


def run_renew_subscription_job(
    database: Database,
    settings: Settings,
    queue: JobQueue,
    job: Job,
    *,
    http_post: HttpPost | None = None,
) -> None:
    """Worker handler for ``renew_subscription``: POST the WebSub form to the hub.

    ``202``/``204`` is "accepted" - the hub then calls the RT1 GET
    verification, which records the granted lease and flips the row to
    ``active``, so a successful subscribe POST leaves the row ``pending``.
    Any other status is recorded as ``failed`` and raised as retryable so the
    queue retries (a subscription that never verifies drifts to ``expired``
    and is resubscribed by the producer).
    """
    subscription_id = job.payload.get("subscription_id")
    mode = job.payload.get("mode")
    if not isinstance(subscription_id, int) or mode not in {"subscribe", "unsubscribe"}:
        raise PermanentJobError("renew_subscription job needs subscription_id and a valid mode")
    base = _callback_base(settings)
    if base is None:
        raise PermanentJobError("renew_subscription needs a public callback base")
    with database.session() as session:
        subscription = session.get(PlatformSubscription, subscription_id)
        if subscription is None:
            raise PermanentJobError(
                f"renew_subscription targets missing subscription {subscription_id}"
            )
        fields = {
            "hub.callback": (
                f"{base}/hooks/youtube/websub/{subscription.id}/{subscription.callback_token}"
            ),
            "hub.topic": str(subscription.topic),
            "hub.mode": str(mode),
            "hub.verify": "async",
        }
        if mode == "subscribe":
            fields["hub.secret"] = subscription.hub_secret or ""
            fields["hub.lease_seconds"] = str(WEBSUB_LEASE_SECONDS)

    post = http_post or _post_form
    status = post(WEBSUB_HUB_URL, fields)

    failure: str | None = None
    with database.session() as session:
        subscription = session.get(PlatformSubscription, subscription_id)
        if subscription is not None:
            if status in (202, 204):
                subscription.last_error = None
                if mode == "unsubscribe":
                    subscription.state = "unsubscribed"
            else:
                failure = f"hub returned HTTP {status}"
                subscription.state = "failed"
                subscription.last_error = failure
    if failure is not None:
        raise ProviderError(f"WebSub {mode} failed: {failure}", retryable=True)
