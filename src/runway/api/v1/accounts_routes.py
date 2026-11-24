"""`/api/v1/accounts/{id}`: incremental publishing consent (spec 08 section 1).

`POST /api/v1/accounts/{id}/publishing/enable` starts an incremental OAuth
flow that adds the publishing scopes an account does not yet have, dispatched
per platform:

- YouTube: the upload scope (and the posts scope when the pilot flag is on).
- TikTok: `video.upload`, and `video.publish` only for an audited app
  (spec 08 section 4).

The issued OAuth state row is bound to the account, so the shared
`/api/platforms/{platform}/oauth/callback` refuses to attach a token whose
platform identity differs (the wrong-channel guard), and stores the scopes
the response actually granted.

`GET /api/v1/accounts/{id}/youtube/categories` lists the assignable YouTube
video categories for the target settings UI.

`GET /api/v1/accounts/{id}/tiktok/creator-info` calls `creator_info` fresh on
every request: the mandatory posting screen is built from exactly what TikTok
returns for this creator right now.
"""

from __future__ import annotations

import json
import re

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

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
from runway.platforms.base import (
    AuthorizationExpired,
    ProviderError,
    RateLimited,
    safe_error_summary,
)
from runway.platforms.capabilities import (
    TIKTOK_SCOPES,
    YOUTUBE_ENGAGE_SCOPE,
    Platform,
)
from runway.platforms.oauth import OAuthStateService
from runway.platforms.post_settings import TikTokVideoSettings, release_notice
from runway.platforms.publish_capability import (
    YOUTUBE_UPLOAD_SCOPE,
    AccountFacts,
    AppStatus,
    account_publishing_capabilities,
)
from runway.platforms.publisher_registry import (
    PublisherRegistry,
    build_publisher_registry,
)
from runway.platforms.registry import ProviderRegistry
from runway.platforms.tiktok import TikTokProvider
from runway.platforms.tokens import pkce_challenge
from runway.platforms.youtube import YouTubeProvider
from runway.posts.types import TargetPostType

TIKTOK_UPLOAD_SCOPE = "video.upload"
TIKTOK_PUBLISH_SCOPE = "video.publish"

_REGION = re.compile(r"^[A-Za-z]{2}$")


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


def _account(session: Session, principal: Principal, account_id: int) -> ConnectedAccount:
    account = session.get(ConnectedAccount, account_id)
    if account is None or account.workspace_id != principal.workspace_id:
        raise _problem(ProblemCode.NOT_FOUND, "this account does not exist")
    session.expunge(account)
    return account


def _granted_scopes(account: ConnectedAccount) -> list[str]:
    try:
        value = json.loads(account.granted_scopes_json or "[]")
    except json.JSONDecodeError:
        return []
    return [str(scope) for scope in value] if isinstance(value, list) else []


def build_accounts_router(
    database: Database,
    settings: Settings,
    *,
    registry: ProviderRegistry | None = None,
    publishers: PublisherRegistry | None = None,
) -> APIRouter:
    router = build_v1_router(tags=["accounts"], prefix="/accounts")
    if registry is None:
        registry = ProviderRegistry(database, settings)
    if publishers is None:
        publishers = build_publisher_registry(settings, database)
    oauth_states = OAuthStateService(database)

    def _enable_tiktok_publishing(
        account: ConnectedAccount, principal: Principal
    ) -> dict[str, object]:
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
            purpose="enable_publishing",
            account_id=account.id,
            workspace_id=principal.workspace_id,
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

    def _enable_youtube_publishing(
        account: ConnectedAccount, principal: Principal
    ) -> dict[str, object]:
        if settings.platform_provider == "mock":
            raise _problem(
                ProblemCode.CAPABILITY_NOT_READY,
                "mock provider mode has no OAuth; its accounts publish in mock mode",
            )
        platform = Platform.youtube
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
            account_id=account.id,
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

    @router.post("/{account_id}/publishing/enable")
    def enable_publishing(
        account_id: int, principal: Principal = Depends(get_principal)
    ) -> dict[str, object]:
        """The incremental-consent URL for this account's missing publishing scopes."""
        require(principal, ACCOUNTS_MANAGE)
        with database.session() as session:
            account = _account(session, principal, account_id)
        platform = Platform(account.platform)
        if platform is Platform.tiktok:
            return _enable_tiktok_publishing(account, principal)
        if platform is Platform.youtube:
            return _enable_youtube_publishing(account, principal)
        raise _problem(
            ProblemCode.VALIDATION_FAILED,
            f"publishing enable for {platform.value} is not built yet",
            platform=platform.value,
        )

    @router.get("/{account_id}/publishing/capability")
    def publishing_capability_view(
        account_id: int, principal: Principal = Depends(get_principal)
    ) -> dict[str, object]:
        """Per-post-type capability in words (`accounts:read`; spec 08 section 5).

        The same rule the post service applies to a target, listed for every
        post type of the account's platform so the owner sees what an "Enable
        publishing" consent would unlock before opening the editor.
        """
        require(principal, ACCOUNTS_READ)
        with database.session() as session:
            account = _account(session, principal, account_id)
        items = account_publishing_capabilities(
            Platform(account.platform),
            account=AccountFacts(
                status=account.status,
                granted_scopes=tuple(_granted_scopes(account)),
            ),
            app=AppStatus.from_settings(settings),
            profile=settings.profile,
            implemented=publishers.names(),
            media_host_available=settings.r2_configured,
            mock=publishers.mock_mode,
        )
        return {
            "account_id": account.id,
            "platform": account.platform,
            "items": [item.as_dict() for item in items],
        }

    @router.get("/{account_id}/youtube/categories")
    def youtube_categories(
        account_id: int,
        region: str = "US",
        principal: Principal = Depends(get_principal),
    ) -> dict[str, object]:
        """Assignable `videoCategories.list` entries for the target settings UI.

        Cached by the provider for a day per region (spec 08 section 2).
        """
        require(principal, ACCOUNTS_READ)
        if not _REGION.fullmatch(region):
            raise _problem(ProblemCode.VALIDATION_FAILED, "region must be a two-letter region code")
        with database.session() as session:
            account = _account(session, principal, account_id)
        platform = Platform(account.platform)
        if platform is not Platform.youtube:
            raise _problem(
                ProblemCode.VALIDATION_FAILED,
                "categories are a YouTube account feature",
            )
        if not account.token_ref:
            raise _problem(
                ProblemCode.CAPABILITY_NOT_READY,
                "the account has no stored token; reconnect it",
            )
        provider = registry.provider(platform)
        if not isinstance(provider, YouTubeProvider):
            raise _problem(
                ProblemCode.CAPABILITY_NOT_READY,
                "categories are unavailable for this provider",
            )
        try:
            token = registry.fresh_token(platform, account.token_ref)
            items = provider.list_video_categories(token, region_code=region.upper())
        except AuthorizationExpired as exc:
            # needs_reauth: the read capability is not ready until the owner
            # reconnects the account.
            raise _problem(
                ProblemCode.CAPABILITY_NOT_READY,
                "the YouTube account must be re-authorized; reconnect it",
            ) from exc
        except RateLimited as exc:
            raise Problem.of(
                ProblemCode.RATE_LIMITED,
                safe_error_summary(exc),
                headers={"Retry-After": "60"},
            ) from exc
        except ProviderError as exc:
            raise _problem(
                ProblemCode.CAPABILITY_NOT_READY,
                safe_error_summary(exc),
            ) from exc
        return {"items": items, "region": region.upper()}

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
        with database.session() as session:
            account = _account(session, principal, account_id)
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
