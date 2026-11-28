"""Middleware and state the hosted profile adds to the API (spec 05 sections 2 and 6).

Starlette runs the middleware added last first, so a request passes:
security headers -> per-address rate limit (requests without a session, and
every request to the signed webhook) -> origin guard -> CORS -> routes, and
`get_principal` then verifies the session and applies the per-user and
per-workspace limits (a token that fails verification spends the address
bucket there).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from runway.api.identity_hook_routes import CLERK_HOOK_PATH
from runway.config import Settings
from runway.hosted.middleware import ClientRateLimit, HostedRequestGuard, SecurityHeaders
from runway.hosted.ratelimit import InProcessTokenBucket, RateLimit
from runway.identity.registry import build_identity_provider
from runway.realtime.meta_hooks import META_DATA_DELETION_PATH, META_DEAUTHORIZE_PATH

# `/api/settings` keys a hosted tenant may see. The rest of public_dict()
# describes the operator's machine (data paths, the browser publisher's
# channel and connector account) and stays private.
HOSTED_SETTINGS_KEYS = frozenset(
    {
        "profile",
        "product_name",
        "timezone",
        "approval_required",
        "publishing_paused",
        "platform_provider",
        "youtube_oauth_configured",
        "instagram_oauth_configured",
        "tiktok_oauth_configured",
        "inbox_replies_enabled",
        "platform_sync_interval_minutes",
    }
)


def configure_hosted(application: FastAPI, settings: Settings) -> None:
    origins = settings.hosted_web_origin_list
    limiter = InProcessTokenBucket()
    application.state.rate_limiter = limiter
    # No network call here; the provider fetches its keys on first use.
    application.state.identity_provider = build_identity_provider(settings)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Authorization", "Content-Type"],
    )
    application.add_middleware(HostedRequestGuard, allowed_origins=origins)
    application.add_middleware(
        ClientRateLimit,
        limiter=limiter,
        limit=RateLimit(settings.rate_limit_ip_per_minute),
        address_only_paths=(
            CLERK_HOOK_PATH,
            META_DATA_DELETION_PATH,
            META_DEAUTHORIZE_PATH,
        ),
    )
    application.add_middleware(SecurityHeaders)


def hosted_settings_view(settings: Settings) -> dict[str, object]:
    return {
        key: value for key, value in settings.public_dict().items() if key in HOSTED_SETTINGS_KEYS
    }
