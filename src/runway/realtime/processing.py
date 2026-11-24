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
from runway.platforms.capabilities import Platform
from runway.platforms.sync import SYNC_ACCOUNT_JOB
from runway.realtime import PROCESS_WEBHOOK_JOB, hooks, standby
from runway.realtime.meta_hooks import (
    META_DATA_DELETION_PROVIDER,
    META_DEAUTHORIZE_PROVIDER,
)
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
        if event.provider in (META_DEAUTHORIZE_PROVIDER, META_DATA_DELETION_PROVIDER):
            _process_meta(session, database, settings, queue, event)
            return
        parsed = _parse_body(event)
        if parsed is _UNPARSABLE:
            _drop(event, "unparsable body")
            return
        accounts = _resolve_accounts(session, event, parsed)
        if not accounts:
            _drop(event, "unknown account")
            return
        # Two workspaces may have connected the same platform identity: every
        # matching account gets its own processing pass below. The delivery
        # row itself can carry one workspace_id, so it is filed under the
        # first resolved account's.
        event.account_id = accounts[0].id
        event.workspace_id = accounts[0].workspace_id
        event_id_out, provider = event.id, event.provider
        targets = [(account.id, account.workspace_id) for account in accounts]
    # Each pass runs bound to that account's workspace: criteria, the write
    # guard and (on Postgres) the RLS GUC all apply to it.
    for account_id, workspace_id in targets:
        with database.session(workspace_id=workspace_id) as session:
            account = session.get(ConnectedAccount, account_id)
            if account is None:
                continue
            _process(session, queue, event_id_out, provider, account, parsed, database)
    # The delivery row lives under the first resolved account's workspace.
    with database.session(workspace_id=targets[0][1]) as session:
        event = session.get(WebhookEvent, event_id_out)
        if event is not None:
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


def _resolve_accounts(
    session: Session, event: WebhookEvent, parsed: object
) -> list[ConnectedAccount]:
    """Every account the delivery hints at - one per workspace that connected
    the same platform identity (spec 01 section 4)."""
    if event.provider == "youtube":
        if event.subscription_id is None:
            return []
        subscription = session.get(PlatformSubscription, event.subscription_id)
        if subscription is None:
            return []
        account = session.get(ConnectedAccount, subscription.account_id)
        return [] if account is None else [account]
    if event.provider == "instagram":
        if not isinstance(parsed, dict):
            return []
        entries = parsed.get("entry")
        if not isinstance(entries, list):
            return []
        seen: set[int] = set()
        accounts: list[ConnectedAccount] = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            external_id = entry.get("id")
            if external_id is None:
                continue
            for account in _accounts_by_external_id(session, "instagram", str(external_id)):
                if account.id not in seen:
                    seen.add(account.id)
                    accounts.append(account)
        return accounts
    if event.provider == "tiktok":
        if not isinstance(parsed, dict):
            return []
        openid = parsed.get("user_openid")
        if openid is None:
            return []
        return _accounts_by_external_id(session, "tiktok", str(openid))
    return []


def _accounts_by_external_id(
    session: Session, platform: str, external_id: str
) -> list[ConnectedAccount]:
    return list(
        session.scalars(
            select(ConnectedAccount)
            .where(
                ConnectedAccount.platform == platform,
                ConnectedAccount.external_account_id == external_id,
            )
            .order_by(ConnectedAccount.id)
        ).all()
    )


def _process(
    session: Session,
    queue: JobQueue,
    event_id: int,
    provider: str,
    account: ConnectedAccount,
    parsed: object,
    database: Database,
) -> None:
    if provider == "tiktok" and isinstance(parsed, dict):
        _process_tiktok(session, queue, event_id, account, parsed, database)
        return
    # youtube (Atom entry, e.g. yt:videoId) and instagram (changes[].field)
    # both mean "this account changed": fetch it, trusting nothing else.
    _enqueue_sync(session, queue, event_id, account)


def _process_tiktok(
    session: Session,
    queue: JobQueue,
    event_id: int,
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
    _enqueue_sync(session, queue, event_id, account)


def _meta_user_id(account: ConnectedAccount) -> str | None:
    """The app-scoped Meta user id a Unified Login account recorded at connect.

    Meta's compliance callbacks name that id, not the Instagram account id
    `external_account_id` holds; both identify the same connection.
    """
    try:
        meta = json.loads(account.metadata_json or "{}")
    except json.JSONDecodeError:
        return None
    value = meta.get("meta_user_id") if isinstance(meta, dict) else None
    return str(value) if isinstance(value, str) and value else None


def _process_meta(
    session: Session,
    database: Database,
    settings: Settings,
    queue: JobQueue,
    event: WebhookEvent,
) -> None:
    """Meta compliance callbacks: detach or scrub the matching account.

    The body is the form field ``signed_request``; the signature was checked
    at ingest and is re-checked here so a secret rotation cannot launder a
    stored forgery. A delivery naming a Meta user we never connected is still
    a valid request - there is simply nothing to delete.
    """
    from runway.platforms.registry import ProviderRegistry
    from runway.realtime.lifecycle import disconnect_account, purge_account_data
    from runway.realtime.meta_hooks import (
        META_DATA_DELETION_PROVIDER,
        _app_secrets,
        _signed_request_field,
        verify_signed_request,
    )

    raw = _signed_request_field(event.body)
    payload = verify_signed_request(raw, _app_secrets(settings)) if raw is not None else None
    if payload is None:
        _drop(event, "signed_request no longer verifies")
        return
    event.processed_at = utcnow()
    user_id = payload.get("user_id")
    if user_id is None:
        event.error = "signed_request carries no user_id"
        return
    accounts = [
        account
        for account in session.scalars(
            select(ConnectedAccount).where(
                ConnectedAccount.platform == Platform.instagram.value,
            )
        ).all()
        if str(user_id) in {account.external_account_id, _meta_user_id(account)}
    ]
    if not accounts:
        # A person may request deletion without ever connecting; nothing held.
        return
    registry = ProviderRegistry(database, settings)
    for account in accounts:
        event.account_id = account.id
        event.workspace_id = account.workspace_id
        if event.provider == META_DATA_DELETION_PROVIDER:
            purge_account_data(
                session,
                registry,
                queue,
                account,
                marker=f"{event.id}:{account.id}",
                actor_kind="platform",
            )
        else:
            disconnect_account(session, registry, queue, account, actor_kind="platform")


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
    session: Session, queue: JobQueue, event_id: int, account: ConnectedAccount
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
        idempotency_key=f"{SYNC_ACCOUNT_JOB}:{account.id}:webhook:{event_id}",
        payload={"account_id": int(account.id), "trigger": "webhook"},
        workspace_id=int(account.workspace_id),
        # A missed hint is covered by the next scheduled sync; do not pile up retries.
        max_attempts=1,
    )
