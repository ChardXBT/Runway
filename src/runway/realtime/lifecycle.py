"""Shared account lifecycle actions (spec 05 sections 4-5).

`disconnect_account` is the one path that detaches a grant: every caller -
the unversioned local route, the v1 route, the Meta deauthorize hook and
the workspace-deletion sweep - revokes at the provider, drops the stored
token, cancels pending sends, runs the subscription teardown and writes
the same audit + activity rows. Callers pass the open session so their
transaction owns the commit.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.orm import Session

from runway.auth.audit import record_audit
from runway.db.models import AuditLogEntry, ConnectedAccount, PublishJob, utcnow
from runway.events.log import record_event
from runway.events.types import ACCOUNT_UPDATED
from runway.jobs.queue import JOB_STATE_MACHINE, JobQueue, JobState
from runway.platforms.base import ProviderError
from runway.platforms.capabilities import Platform
from runway.realtime import subscriptions

if TYPE_CHECKING:
    from runway.auth.principal import Principal
    from runway.platforms.registry import ProviderRegistry

logger = logging.getLogger(__name__)


def cancel_pending_account_jobs(session: Session, account_id: int) -> int:
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


def revoke_and_delete_token(
    session: Session, registry: ProviderRegistry, account: ConnectedAccount
) -> str:
    """Best-effort provider revoke, then the token-store delete. Returns the outcome."""
    revoke = "no_token"
    token = None
    if account.token_ref:
        token = registry.token_store.load(str(account.token_ref))
    if token is not None:
        try:
            provider = registry.provider(Platform(account.platform))
            revoke = "revoked" if provider.revoke(token) else "unsupported"
        except ProviderError as exc:
            revoke = "failed"
            logger.warning("provider revoke failed for account %s: %s", account.id, exc.detail)
    if account.token_ref:
        registry.token_store.delete(str(account.token_ref))
        if revoke == "no_token":
            revoke = "missing"
    return revoke


def disconnect_account(
    session: Session,
    registry: ProviderRegistry,
    queue: JobQueue,
    account: ConnectedAccount,
    *,
    actor: Principal | None = None,
    actor_kind: str | None = None,
) -> dict[str, object]:
    """Detach one account's grant; the caller's session owns the commit.

    `revoke` reports the provider call: "revoked", "unsupported" (the
    provider has no remote revoke), "failed" (ProviderError, logged), or
    "no_token"/"missing" when there was nothing stored to revoke.

    `actor` is the requesting principal when a person or key asked; a
    platform-initiated disconnect (Meta's compliance callbacks) has no
    principal, so it passes `actor_kind` - a free string on the audit row,
    like the "oauth" actor the connect side writes. `actor` wins when both
    are given.
    """
    revoke = revoke_and_delete_token(session, registry, account)
    cancelled = cancel_pending_account_jobs(session, account.id)
    account.status = "disconnected"
    account.token_ref = None
    account.last_error = None
    subscriptions.on_account_disconnected(session, queue, account)
    if actor is not None:
        record_audit(
            session,
            actor,
            "account.disconnect",
            target_type="account",
            target_id=account.id,
            detail={"platform": account.platform, "revoke": revoke},
        )
    elif actor_kind is not None:
        session.add(
            AuditLogEntry(
                workspace_id=account.workspace_id,
                actor_kind=actor_kind,
                actor_id=account.platform,
                action="account.disconnect",
                target_type="account",
                target_id=str(account.id),
                detail_json=json.dumps(
                    {"platform": account.platform, "revoke": revoke}, sort_keys=True
                ),
                created_at=utcnow(),
            )
        )
    record_event(
        session,
        account.workspace_id,
        ACCOUNT_UPDATED,
        "account",
        account.id,
        {"status": "disconnected", "revoke": revoke},
    )
    return {
        "account_id": account.id,
        "status": "disconnected",
        "revoke": revoke,
        "cancelled_jobs": cancelled,
    }


def purge_account_data(
    session: Session,
    registry: ProviderRegistry,
    queue: JobQueue,
    account: ConnectedAccount,
    *,
    marker: str,
    actor_kind: str | None = None,
) -> dict[str, object]:
    """Erase what the workspace holds about one platform identity.

    Meta's data-deletion callback targets a Meta user, not a request: the
    grant is detached like a disconnect, then the columns that identify the
    person (handle, display name, avatar, profile link, external id) are
    scrubbed so a later re-list returns nothing personal. ``marker`` must be
    unique per request - the external id column is part of a unique
    constraint, so scrubbed rows each get their own placeholder.
    """
    result = disconnect_account(session, registry, queue, account, actor_kind=actor_kind)
    account.external_account_id = f"purged:{marker}"
    account.handle = None
    account.display_name = None
    account.avatar_url = None
    account.profile_url = None
    account.account_type = None
    account.granted_scopes_json = "[]"
    account.metadata_json = "{}"
    record_event(
        session,
        account.workspace_id,
        ACCOUNT_UPDATED,
        "account",
        account.id,
        {"status": "data_purged"},
    )
    return result
