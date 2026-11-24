"""`/api/v1/accounts/{id}`: incremental publishing consent (spec 08 section 1).

`POST /api/v1/accounts/{id}/publishing/enable` starts an incremental OAuth
flow that adds the upload scope (and the posts scope when the pilot flag is
on) to a connected account. The state row is bound to the account, so the
shared `/api/platforms/{platform}/oauth/callback` refuses to attach a token
whose channel differs (the wrong-channel guard), and stores the scopes the
response actually granted.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from runway.api.v1.router import build_v1_router
from runway.auth.principal import ACCOUNTS_MANAGE, Principal, get_principal, require
from runway.auth.problems import Problem, ProblemCode
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ConnectedAccount
from runway.platforms.capabilities import YOUTUBE_ENGAGE_SCOPE, Platform
from runway.platforms.oauth import OAuthStateService
from runway.platforms.publish_capability import YOUTUBE_UPLOAD_SCOPE
from runway.platforms.registry import ProviderRegistry
from runway.platforms.tokens import pkce_challenge
from runway.platforms.youtube import YouTubeProvider


def _problem(code: ProblemCode, detail: str) -> Problem:
    return Problem.of(code, detail)


def _account(
    session: Session, principal: Principal, account_id: int
) -> ConnectedAccount:
    account = session.get(ConnectedAccount, account_id)
    if account is None or account.workspace_id != principal.workspace_id:
        raise _problem(ProblemCode.NOT_FOUND, "this account does not exist")
    session.expunge(account)
    return account


def build_accounts_router(database: Database, settings: Settings) -> APIRouter:
    router = build_v1_router(tags=["accounts"], prefix="/accounts")
    registry = ProviderRegistry(database, settings)
    oauth_states = OAuthStateService(database)

    @router.post("/{account_id}/publishing/enable")
    def enable_publishing(
        account_id: int, principal: Principal = Depends(get_principal)
    ) -> dict[str, object]:
        """The incremental-consent URL for this account's missing scopes."""
        require(principal, ACCOUNTS_MANAGE)
        with database.session() as session:
            account = _account(session, principal, account_id)
        platform = Platform(account.platform)
        if platform is not Platform.youtube:
            raise _problem(
                ProblemCode.VALIDATION_FAILED,
                f"publishing enable for {platform.value} is not built yet",
            )
        if settings.platform_provider == "mock":
            raise _problem(
                ProblemCode.CAPABILITY_NOT_READY,
                "mock provider mode has no OAuth; its accounts publish in mock mode",
            )
        provider = registry.provider(platform)
        if not isinstance(provider, YouTubeProvider):
            raise _problem(
                ProblemCode.CAPABILITY_NOT_READY,
                "publishing enable is unavailable for this provider",
            )
        if not provider.configured():
            raise _problem(
                ProblemCode.CAPABILITY_NOT_READY,
                "YouTube OAuth is not configured; set the client credentials",
            )
        extra = [YOUTUBE_UPLOAD_SCOPE]
        # youtube.force-ssl only when the build can use the posts API; asking
        # for it otherwise buys the consent screen a scary scope for nothing.
        if settings.youtube_posts_api_access:
            extra.append(YOUTUBE_ENGAGE_SCOPE)
        redirect_uri = registry.redirect_uri(platform)
        issued = oauth_states.issue(
            platform,
            redirect_uri=redirect_uri,
            use_pkce=True,
            purpose="enable_publishing",
            account_id=account_id,
            workspace_id=principal.workspace_id,
        )
        authorization_url = provider.authorization_url(
            state=issued.state,
            redirect_uri=redirect_uri,
            code_challenge=(pkce_challenge(issued.code_verifier) if issued.code_verifier else None),
            extra_scopes=extra,
        )
        return {
            "authorization_url": authorization_url,
            "state": issued.state,
            "requested_scopes": extra,
        }

    return router
