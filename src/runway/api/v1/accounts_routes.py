"""`GET /api/v1/accounts/{id}/sync-status` (spec 04 section 5, RT2 part D).

The Accounts page's freshness panel: every sync scope's last outcome and next
due time, whether a platform push subscription is live, today's provider
quota use, and whether the adaptive scheduler is enabled.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select

from runway.api.v1.router import build_v1_router
from runway.auth.principal import ACCOUNTS_READ, Principal, get_principal, require
from runway.auth.problems import Problem, ProblemCode
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import AccountSyncState, ConnectedAccount, PlatformSubscription
from runway.platforms.base import iso_utc
from runway.realtime.quota import provider_quota_summary
from runway.realtime.schedule import SCOPES


def _aware(moment: datetime | None) -> datetime | None:
    if moment is None:
        return None
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _scope_entry(state: AccountSyncState | None) -> dict[str, object]:
    if state is None:
        return {
            "last_run_at": None,
            "last_status": None,
            "last_error": None,
            "next_run_at": None,
            "boosted_until": None,
            "source": "poll",
        }
    return {
        "last_run_at": iso_utc(_aware(state.last_run_at)),
        "last_status": state.last_status,
        "last_error": state.last_error,
        "next_run_at": iso_utc(_aware(state.next_run_at)),
        "boosted_until": iso_utc(_aware(state.boosted_until)),
        "source": state.source or "poll",
    }


def build_accounts_router(database: Database, settings: Settings) -> APIRouter:
    router = build_v1_router(tags=["accounts"], prefix="/accounts")

    @router.get("/{account_id}/sync-status")
    def sync_status(
        account_id: int,
        principal: Principal = Depends(get_principal),
    ) -> dict[str, object]:
        """Per-scope sync freshness, push status and today's quota use."""
        require(principal, ACCOUNTS_READ)
        with database.session() as session:
            account = session.get(ConnectedAccount, account_id)
            # Another workspace's account is a 404, never a 403 that
            # confirms it exists.
            if account is None or int(account.workspace_id) != principal.workspace_id:
                raise Problem.of(ProblemCode.NOT_FOUND, "no such account")
            states = session.scalars(
                select(AccountSyncState).where(AccountSyncState.account_id == account.id)
            ).all()
            by_scope = {state.scope: state for state in states}
            push_enabled = (
                session.scalar(
                    select(PlatformSubscription.id)
                    .where(
                        PlatformSubscription.account_id == account.id,
                        PlatformSubscription.state == "active",
                    )
                    .limit(1)
                )
                is not None
            )
            quota = provider_quota_summary(session, str(account.platform), settings)
        return {
            "account_id": int(account.id),
            "platform": str(account.platform),
            "adaptive_sync_enabled": settings.adaptive_sync_enabled,
            "push_enabled": push_enabled,
            "quota": quota,
            "scopes": [{"scope": scope, **_scope_entry(by_scope.get(scope))} for scope in SCOPES],
        }

    return router
