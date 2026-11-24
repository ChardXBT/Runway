"""`/api/v1/accounts` account routes: incremental consent and the TikTok screen.

`POST /api/v1/accounts/{id}/publishing/enable` starts a re-consent that adds
the publishing scopes an account does not yet have (TikTok `video.upload`, and
`video.publish` only for an audited app — spec 08 section 1). The issued OAuth
state names the account, so the shared callback refuses to attach the grant to
any other identity.

`GET /api/v1/accounts/{id}/tiktok/creator-info` calls `creator_info` fresh on
every request: the mandatory posting screen is built from exactly what TikTok
returns for this creator right now.
"""

from __future__ import annotations

import json

from fastapi import Depends
from fastapi.routing import APIRouter

from runway.api.v1.router import build_v1_router
from runway.auth.principal import (
    ACCOUNTS_MANAGE,
    ACCOUNTS_READ,
    Principal,
    get_principal,
    require,
)
from runway.auth.problems import Problem, ProblemCode
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ConnectedAccount
from runway.platforms.base import AuthorizationExpired, ProviderError, RateLimited
from runway.platforms.capabilities import TIKTOK_SCOPES, Platform
from runway.platforms.oauth import OAuthStateService
from runway.platforms.post_settings import TikTokVideoSettings, release_notice
from runway.platforms.registry import ProviderRegistry
from runway.platforms.tiktok import TikTokProvider
from runway.posts.types import TargetPostType

TIKTOK_UPLOAD_SCOPE = "video.upload"
TIKTOK_PUBLISH_SCOPE = "video.publish"


def _problem(code: ProblemCode, detail: str, **extra: object) -> Problem:
    return Problem.of(code, detail, extra=extra or None)


def _upstream(exc: ProviderError) -> Problem:
    return Problem(
        502,
        "upstream_error",
        "Upstream error",
        f"TikTok answered: {exc.detail}",
        extra={"retryable": exc.retryable},
    )


def build_accounts_router(
    database: Database,
    settings: Settings,
    *,
    registry: ProviderRegistry | None = None,
) -> APIRouter:
    router = build_v1_router(prefix="/accounts", tags=["accounts"])
    if registry is None:
        registry = ProviderRegistry(database, settings)
    oauth_states = OAuthStateService(database)

    def _account_or_404(account_id: int, workspace_id: int) -> ConnectedAccount:
        with database.session() as session:
            account = session.get(ConnectedAccount, account_id)
            if account is None or account.workspace_id != workspace_id:
                raise _problem(ProblemCode.NOT_FOUND, "account not found")
            session.expunge(account)
            return account

    def _granted_scopes(account: ConnectedAccount) -> list[str]:
        try:
            value = json.loads(account.granted_scopes_json or "[]")
        except json.JSONDecodeError:
            return []
        return [str(scope) for scope in value] if isinstance(value, list) else []

    @router.post("/{account_id}/publishing/enable")
    def enable_publishing(
        account_id: int, principal: Principal = Depends(get_principal)
    ) -> dict[str, object]:
        """Start the incremental consent that adds publishing scopes (`accounts:manage`).

        Returns `consent_required` with the authorization URL to open, or
        `already_enabled` when the account already holds every scope it needs.
        """
        require(principal, ACCOUNTS_MANAGE)
        account = _account_or_404(account_id, principal.workspace_id)
        if account.platform != Platform.tiktok.value:
            raise _problem(
                ProblemCode.VALIDATION_FAILED,
                "incremental publishing consent is implemented for TikTok accounts",
                platform=account.platform,
            )
        if settings.platform_provider == "mock":
            raise _problem(
                ProblemCode.VALIDATION_FAILED,
                "publishing consent is unavailable in mock provider mode",
            )
        provider = registry.provider(Platform.tiktok)
        if not isinstance(provider, TikTokProvider) or not provider.configured():
            raise _problem(
                ProblemCode.CAPABILITY_NOT_READY,
                "TikTok OAuth is not configured",
            )
        needed = [TIKTOK_UPLOAD_SCOPE]
        # `video.publish` is asked for only by an audited app (spec 08 §4);
        # anything less never sees the scope.
        if settings.tiktok_app_status == "audited":
            needed.append(TIKTOK_PUBLISH_SCOPE)
        granted = _granted_scopes(account)
        missing = [scope for scope in needed if scope not in granted]
        if not missing:
            return {"status": "already_enabled", "account_id": account.id}
        # Request the read scopes plus the publishing scopes the consent adds;
        # TikTok keeps scopes already granted on the account.
        requested = list(dict.fromkeys([*TIKTOK_SCOPES, *needed, *granted]))
        issued = oauth_states.issue(
            Platform.tiktok,
            redirect_uri=registry.redirect_uri(Platform.tiktok),
            use_pkce=False,
            for_account_id=account.id,
        )
        return {
            "status": "consent_required",
            "account_id": account.id,
            "authorization_url": provider.authorization_url(
                state=issued.state,
                redirect_uri=registry.redirect_uri(Platform.tiktok),
                code_challenge=None,
                scopes=requested,
            ),
            "state": issued.state,
            "scopes": requested,
        }

    @router.get("/{account_id}/tiktok/creator-info")
    def creator_info(
        account_id: int, principal: Principal = Depends(get_principal)
    ) -> dict[str, object]:
        """Fresh `creator_info` for the mandatory posting screen (`accounts:read`).

        TikTok requires this call before every publish: privacy options,
        interaction availability, maximum duration, and the exact consent
        strings the screen must show.
        """
        require(principal, ACCOUNTS_READ)
        account = _account_or_404(account_id, principal.workspace_id)
        if account.platform != Platform.tiktok.value:
            raise _problem(ProblemCode.NOT_FOUND, "not a TikTok account")
        if settings.platform_provider == "mock":
            raise _problem(
                ProblemCode.VALIDATION_FAILED,
                "creator info is unavailable in mock provider mode",
            )
        if not account.token_ref:
            raise _problem(
                ProblemCode.VALIDATION_FAILED,
                "reconnect the account first; it holds no token",
            )
        provider = registry.provider(Platform.tiktok)
        if not isinstance(provider, TikTokProvider) or not provider.configured():
            raise _problem(
                ProblemCode.CAPABILITY_NOT_READY,
                "TikTok OAuth is not configured",
            )
        try:
            token = registry.fresh_token(Platform.tiktok, account.token_ref)
        except AuthorizationExpired as exc:
            raise _problem(
                ProblemCode.VALIDATION_FAILED,
                "the account's authorization expired; reconnect it",
            ) from exc
        except RateLimited as exc:
            raise _problem(ProblemCode.RATE_LIMITED, exc.detail) from exc
        try:
            info = provider.creator_info(token)
        except AuthorizationExpired as exc:
            raise _problem(
                ProblemCode.VALIDATION_FAILED,
                "the account's authorization expired; reconnect it",
            ) from exc
        except RateLimited as exc:
            raise _problem(ProblemCode.RATE_LIMITED, exc.detail) from exc
        except ProviderError as exc:
            raise _upstream(exc) from exc
        return {
            "account_id": account.id,
            "creator_nickname": info.creator_nickname,
            "creator_username": info.creator_username,
            "creator_avatar_url": info.creator_avatar_url,
            "privacy_level_options": info.privacy_level_options,
            "comment_disabled": info.comment_disabled,
            "duet_disabled": info.duet_disabled,
            "stitch_disabled": info.stitch_disabled,
            "max_video_post_duration_sec": info.max_video_post_duration_sec,
            "consent": {
                "default": release_notice(TargetPostType.TIKTOK_VIDEO, TikTokVideoSettings()),
                "branded": release_notice(
                    TargetPostType.TIKTOK_VIDEO,
                    TikTokVideoSettings(commercial_content=True, branded_content=True),
                ),
            },
        }

    return router
