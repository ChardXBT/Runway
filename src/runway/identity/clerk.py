"""Clerk session-token verification.

Clerk issues a short-lived RS256 session JWT, sent as `Authorization: Bearer`
by the web server and as the `__session` cookie by the browser. It is verified
locally against the instance's JWKS; nothing is stored in the API.

Active-organization claims. Clerk has used two forms, and both are read:

* session token v2 (`"v": 2`, Clerk's current default): a compact object
  `"o": {"id": "org_...", "rol": "admin", "slg": "acme"}`;
* session token v1: top-level `org_id`, `org_role` (`"org:admin"`) and
  `org_slug`.

When both are present, `o.id` wins. The role is reported without its `org:`
prefix and is informational only: Runway's roles live in `workspace_members`.

Clerk's default session token carries no email or name. If the instance's
session token is customised with `"email": "{{user.primary_email_address}}"`
and `"name": "{{user.full_name}}"`, those are mirrored on sign-in; otherwise
the `user.*` webhooks fill them in.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from starlette.requests import HTTPConnection

from runway.auth.principal import API_KEY_PREFIX
from runway.identity.base import (
    ExternalIdentity,
    IdentityRejected,
    presented_credential,
)
from runway.identity.jwt import (
    ClaimRules,
    Clock,
    JwksCache,
    JwksFetcher,
    httpx_jwks_fetcher,
    verify_rs256,
)

PROVIDER_NAME = "clerk"


@dataclass(frozen=True)
class ClerkConfig:
    issuer: str
    jwks_url: str
    authorized_parties: frozenset[str]
    audience: str | None = None
    leeway_seconds: int = 5


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _role(value: object) -> str | None:
    role = _text(value)
    if role is None:
        return None
    return role.removeprefix("org:")


def organization_claims(claims: Mapping[str, Any]) -> tuple[str | None, str | None, str | None]:
    """Return (organization id, role, slug) from a v2 `o` claim or v1 `org_*` claims."""
    compact = claims.get("o")
    if isinstance(compact, Mapping):
        organization_id = _text(compact.get("id"))
        if organization_id is not None:
            return organization_id, _role(compact.get("rol")), _text(compact.get("slg"))
    organization_id = _text(claims.get("org_id"))
    if organization_id is None:
        return None, None, None
    return organization_id, _role(claims.get("org_role")), _text(claims.get("org_slug"))


class ClerkIdentityProvider:
    name = PROVIDER_NAME

    def __init__(
        self,
        config: ClerkConfig,
        *,
        fetcher: JwksFetcher = httpx_jwks_fetcher,
        monotonic: Clock | None = None,
        wall_clock: Clock | None = None,
    ) -> None:
        self.config = config
        self._rules = ClaimRules(
            issuer=config.issuer,
            authorized_parties=config.authorized_parties,
            audience=config.audience,
            leeway_seconds=config.leeway_seconds,
        )
        self._keys = JwksCache(
            url=config.jwks_url, fetcher=fetcher, clock=monotonic or time.monotonic
        )
        self._wall_clock = wall_clock

    def verify(self, request: HTTPConnection) -> ExternalIdentity:
        presented = presented_credential(request.headers)
        if presented is None:
            raise IdentityRejected("authentication_required", "sign in to use this workspace")
        _source, token = presented
        if token.startswith(API_KEY_PREFIX):
            raise IdentityRejected("invalid_token", "an API key is not a session token")
        now = self._wall_clock() if self._wall_clock is not None else None
        claims = verify_rs256(token, self._keys, self._rules, now=now)
        status = claims.get("sts")
        if status is not None and status != "active":
            # A pending session (for example one that must still choose an
            # organization) is treated as signed out, as Clerk's SDKs do.
            raise IdentityRejected("session_pending", "finish signing in before using Runway")
        organization_id, organization_role, organization_slug = organization_claims(claims)
        return ExternalIdentity(
            provider=PROVIDER_NAME,
            user_id=str(claims["sub"]),
            email=_text(claims.get("email")),
            display_name=_text(claims.get("name")),
            organization_id=organization_id,
            organization_role=organization_role,
            organization_slug=organization_slug,
            session_id=_text(claims.get("sid")),
        )
