"""Incremental consent for Instagram publishing (spec 08 section 3).

`POST /api/v1/accounts/{id}/publishing/enable` is the "turn on publishing"
action: it issues an Instagram Login OAuth URL that asks for the base scopes
plus `instagram_business_content_publish`, and returns the state so the
client can watch it. The OAuth callback (the unversioned platform router)
then stores the granted scopes on the account. An account that already has
the scope — under either the Business Login or the Unified Login name —
reports `already_enabled` instead of issuing another consent round.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException

from runway.api.v1.router import build_v1_router
from runway.auth.principal import (
    ACCOUNTS_MANAGE,
    Principal,
    get_principal,
    require,
)
from runway.auth.problems import Problem, ProblemCode
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ConnectedAccount
from runway.platforms.capabilities import (
    INSTAGRAM_SCOPES,
    Platform,
    equivalent_scopes,
)
from runway.platforms.instagram import InstagramProvider
from runway.platforms.oauth import OAuthStateService
from runway.platforms.publish_capability import INSTAGRAM_PUBLISH_SCOPE
from runway.platforms.registry import ProviderRegistry


def instagram_publish_scopes() -> list[str]:
    """The scope list a publishing-enable consent asks for, in order."""
    return [*INSTAGRAM_SCOPES, INSTAGRAM_PUBLISH_SCOPE]


def build_accounts_router(
    database: Database,
    settings: Settings,
    *,
    registry: ProviderRegistry | None = None,
    oauth_states: OAuthStateService | None = None,
) -> APIRouter:
    registry = registry or ProviderRegistry(database, settings)
    oauth_states = oauth_states or OAuthStateService(database)
    router = build_v1_router(tags=["accounts"], prefix="/accounts")

    @router.post("/{account_id}/publishing/enable")
    def enable_publishing(
        account_id: int, principal: Principal = Depends(get_principal)
    ) -> dict[str, object]:
        require(principal, ACCOUNTS_MANAGE)
        if settings.platform_provider == "mock":
            raise HTTPException(
                status_code=400,
                detail="publishing enable is unavailable in mock provider mode",
            )
        with database.session() as session:
            account = session.get(ConnectedAccount, account_id)
            if account is None or account.workspace_id != principal.workspace_id:
                raise Problem.of(ProblemCode.NOT_FOUND, "no such account")
            platform = account.platform
            granted = json.loads(account.granted_scopes_json or "[]")
            account_status = account.status
        if platform != Platform.instagram.value:
            raise Problem(
                409,
                "publishing_enable_unsupported",
                "Publishing enable is only for Instagram",
                f"account {account_id} is a {platform} account; incremental "
                "consent is implemented for Instagram only",
            )
        wanted = instagram_publish_scopes()
        if account_status == "connected" and INSTAGRAM_PUBLISH_SCOPE in equivalent_scopes(granted):
            return {
                "status": "already_enabled",
                "account_id": account_id,
                "requested_scopes": wanted,
            }
        provider = registry.provider(Platform.instagram)
        if not provider.configured() or not isinstance(provider, InstagramProvider):
            raise Problem(
                409,
                "oauth_not_configured",
                "Instagram OAuth is not configured",
                "set RUNWAY_INSTAGRAM_APP_ID and RUNWAY_INSTAGRAM_APP_SECRET first",
            )
        redirect_uri = registry.redirect_uri(Platform.instagram)
        issued = oauth_states.issue(
            Platform.instagram,
            redirect_uri=redirect_uri,
            use_pkce=False,
            payload=json.dumps({"scopes": wanted}),
        )
        return {
            "status": "authorization_required",
            "account_id": account_id,
            "requested_scopes": wanted,
            "authorization_url": provider.authorization_url(
                state=issued.state,
                redirect_uri=redirect_uri,
                code_challenge=None,
                scopes=wanted,
            ),
            "state": issued.state,
        }

    return router
