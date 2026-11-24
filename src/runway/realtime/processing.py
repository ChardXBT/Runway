"""The `process_webhook` job: turns a verified raw delivery into targeted work.

A webhook is a hint, never data (spec 04 section 2): nothing the platform sent
is written to platform tables. The handler resolves which account the hint is
about and enqueues the fetch or state change it points at. Failures propagate
so the queue retries; whatever cannot be resolved is marked on the event.
"""

from __future__ import annotations

import json
import logging
from typing import Any

# Untrusted webhook bodies are parsed with defusedxml only (a hard dependency);
# the stdlib parser is never a fallback for platform-supplied XML.
from defusedxml import ElementTree as _ElementTree  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.orm import Session

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    ConnectedAccount,
    PlatformSubscription,
    PublishJob,
    WebhookEvent,
    utcnow,
)
from runway.events.log import record_event
from runway.events.types import ACCOUNT_UPDATED
from runway.jobs.queue import JOB_STATE_MACHINE, Job, JobQueue, JobState
from runway.jobs.worker import PermanentJobError
from runway.platforms.sync import SYNC_ACCOUNT_JOB
from runway.realtime import PROCESS_WEBHOOK_JOB, hooks, standby
from runway.realtime.schedule import (
    INVENTORY_BOOST_MINUTES,
    RECENT_METRICS_BOOST_MINUTES,
    boost,
)

logger = logging.getLogger(__name__)

__all__ = ["PROCESS_WEBHOOK_JOB", "run_process_webhook_job"]

_UNPARSABLE = object()
_ERROR_LIMIT = 500


def run_process_webhook_job(
    database: Database, settings: Settings, queue: JobQueue, job: Job
) -> None:
    # Spec 07 section 4: a standby machine runs no platform writes.
    standby.ensure_active(settings)
    event_id = job.payload.get("event_id")
    if not isinstance(event_id, int):
        raise PermanentJobError("process_webhook job has no integer event_id")
    # Resolution is cross-workspace system work: the delivery's workspace is
    # what this pass discovers, so it cannot run inside a workspace binding.
    with database.system_session() as session:
        event = session.get(WebhookEvent, event_id)
        if event is None or event.processed_at is not None:
            # Purged already, or a claim raced a completed run: nothing to do.
            return
        if not event.signature_ok:
            # Routes never enqueue unverified events; if a job points at one
            # anyway, it is noise, not a hint.
            _drop(event, "unverified signature")
            return
        parsed = _parse_body(event)
        if parsed is _UNPARSABLE:
            _drop(event, "unparsable body")
            return
        account = _resolve_account(session, event, parsed)
        if account is None:
            _drop(event, "unknown account")
            return
        event.account_id = account.id
        event.workspace_id = account.workspace_id
        event_id_out, account_id, workspace_id = event.id, account.id, account.workspace_id
    # Processing runs bound to the resolved workspace: criteria, the write
    # guard and (on Postgres) the RLS GUC all apply to it.
    with database.session(workspace_id=workspace_id) as session:
        event = session.get(WebhookEvent, event_id_out)
        account = session.get(ConnectedAccount, account_id)
        if event is None or account is None:
            return
        _process(session, queue, event, account, parsed, database)
        event.processed_at = utcnow()


def _drop(event: WebhookEvent, error: str) -> None:
    event.error = error[:_ERROR_LIMIT]
    event.processed_at = utcnow()


def _parse_body(event: WebhookEvent) -> object:
    if event.provider == "youtube":
        try:
            return _ElementTree.fromstring(event.body)
        except Exception:  # noqa: BLE001 - any XML failure means the hint is unreadable
            return _UNPARSABLE
    try:
        return json.loads(event.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _UNPARSABLE


def _resolve_account(
    session: Session, event: WebhookEvent, parsed: object
) -> ConnectedAccount | None:
    if event.provider == "youtube":
        if event.subscription_id is None:
            return None
        subscription = session.get(PlatformSubscription, event.subscription_id)
        if subscription is None:
            return None
        return session.get(ConnectedAccount, subscription.account_id)
    if event.provider == "instagram":
        if not isinstance(parsed, dict):
            return None
        entries = parsed.get("entry")
        if not isinstance(entries, list):
            return None
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            external_id = entry.get("id")
            if external_id is None:
                continue
            account = _account_by_external_id(session, "instagram", str(external_id))
            if account is not None:
                return account
        return None
    if event.provider == "tiktok":
        if not isinstance(parsed, dict):
            return None
        openid = parsed.get("user_openid")
        if openid is None:
            return None
        return _account_by_external_id(session, "tiktok", str(openid))
    return None


def _account_by_external_id(
    session: Session, platform: str, external_id: str
) -> ConnectedAccount | None:
    return session.scalar(
        select(ConnectedAccount)
        .where(
            ConnectedAccount.platform == platform,
            ConnectedAccount.external_account_id == external_id,
        )
        .limit(1)
    )


def _process(
    session: Session,
    queue: JobQueue,
    event: WebhookEvent,
    account: ConnectedAccount,
    parsed: object,
    database: Database,
) -> None:
    if event.provider == "tiktok" and isinstance(parsed, dict):
        _process_tiktok(session, queue, event, account, parsed, database)
        return
    # youtube (Atom entry, e.g. yt:videoId) and instagram (changes[].field)
    # both mean "this account changed": fetch it, trusting nothing else.
    _enqueue_sync(session, queue, event, account)


def _process_tiktok(
    session: Session,
    queue: JobQueue,
    event: WebhookEvent,
    account: ConnectedAccount,
    parsed: dict[str, Any],
    database: Database,
) -> None:
    name = parsed.get("event")
    if name == "authorization.removed":
        # The same permanent-auth state a failed token refresh uses.
        account.status = "needs_reauth"
        account.last_error = "platform authorization removed (webhook)"
        _cancel_pending_jobs(session, account.id)
        record_event(
            session,
            account.workspace_id,
            ACCOUNT_UPDATED,
            "account",
            account.id,
            {"status": "needs_reauth", "reason": "authorization.removed"},
        )
        return
    if isinstance(name, str) and name.startswith("post.publish."):
        hooks.on_publish_event(session, database, account.id, _publish_id(parsed), name)
        # A publish is also a push hint for the publish-adjacent scopes
        # (spec 04 section 3): inventory for 30 min, recent metrics for 2 h.
        boost(session, account.id, "inventory", INVENTORY_BOOST_MINUTES)
        boost(session, account.id, "content_metrics_recent", RECENT_METRICS_BOOST_MINUTES)
        return
    _enqueue_sync(session, queue, event, account)


def _publish_id(parsed: dict[str, Any]) -> str | None:
    content = parsed.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            return None
    if isinstance(content, dict):
        publish_id = content.get("publish_id")
        return None if publish_id is None else str(publish_id)
    return None


def _cancel_pending_jobs(session: Session, account_id: int) -> int:
    """Cancel pending queue rows for the account; the queue has no account column."""
    rows = session.scalars(
        select(PublishJob).where(PublishJob.state == JobState.PENDING.value)
    ).all()
    cancelled = 0
    for row in rows:
        try:
            payload = json.loads(row.payload_json or "{}")
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("account_id") == account_id:
            JOB_STATE_MACHINE.apply(row, JobState.CANCELLED)
            cancelled += 1
    return cancelled


def _enqueue_sync(
    session: Session, queue: JobQueue, event: WebhookEvent, account: ConnectedAccount
) -> None:
    """The scheduler's payload shape, keyed so webhook retries cannot duplicate it.

    A verified hint also boosts `inventory` for 30 minutes (spec 04 section
    3): the adaptive scheduler then keeps the scope on a 2-minute cadence
    marked `push`.
    """
    boost(session, int(account.id), "inventory", INVENTORY_BOOST_MINUTES)
    queue.enqueue_pending_job(
        session,
        SYNC_ACCOUNT_JOB,
        idempotency_key=f"{SYNC_ACCOUNT_JOB}:{account.id}:webhook:{event.id}",
        payload={"account_id": int(account.id), "trigger": "webhook"},
        workspace_id=int(account.workspace_id),
        # A missed hint is covered by the next scheduled sync; do not pile up retries.
        max_attempts=1,
    )
