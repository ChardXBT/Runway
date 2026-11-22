"""The identity-provider seam (spec 05 section 2).

The API depends only on `IdentityProvider.verify(request) -> ExternalIdentity`.
Provider-specific code (token formats, claim names, webhook payloads) stays in
this package; nothing outside `runway.identity` imports a provider module's
types.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from starlette.datastructures import Headers
from starlette.requests import HTTPConnection

# Agent keys (`rwy_` bearers) are never session tokens; the API-key path resolves them.
from runway.auth.principal import API_KEY_PREFIX

# The cookie the web app's identity provider sets for same-origin calls. Clerk
# uses `__session`; another provider would map its own cookie to this seam.
SESSION_COOKIE = "__session"

CredentialSource = Literal["bearer", "cookie"]


class IdentityRejected(Exception):
    """The request carries no acceptable identity.

    `code` is a stable machine-readable reason. The API turns this into a 401
    problem without echoing the token or the verifier's internals.
    """

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


class IdentityUnavailable(Exception):
    """The provider's keys could not be loaded; the request may succeed later (503)."""


@dataclass(frozen=True, slots=True)
class ExternalIdentity:
    """Who the provider says the caller is, in provider-neutral terms.

    `organization_role` is the provider's own role name with any `org:` prefix
    removed (for example `admin` or `member`). Runway does not grant anything
    from it: Runway roles live in `workspace_members`.
    """

    provider: str
    user_id: str
    email: str | None
    display_name: str | None
    organization_id: str | None
    organization_role: str | None
    organization_slug: str | None = None
    organization_name: str | None = None
    session_id: str | None = None


@runtime_checkable
class IdentityProvider(Protocol):
    """Verifies a request's session credential and says who the caller is."""

    name: str

    def verify(self, request: HTTPConnection) -> ExternalIdentity:
        """Return the caller's identity or raise `IdentityRejected`/`IdentityUnavailable`."""
        ...


def presented_credential(headers: Headers) -> tuple[CredentialSource, str] | None:
    """Return where the session credential comes from, and the credential.

    `Authorization: Bearer` wins over the cookie, so a request that carries a
    bearer is never authenticated by an ambient cookie. An `rwy_` bearer is an
    agent key and is not a session credential; callers check for it first.
    """
    authorization = headers.get("authorization", "")
    scheme, _, credential = authorization.partition(" ")
    if scheme.casefold() == "bearer" and credential.strip():
        return "bearer", credential.strip()
    cookie = session_cookie(headers)
    if cookie:
        return "cookie", cookie
    return None


def session_cookie(headers: Headers) -> str | None:
    raw = headers.get("cookie")
    if not raw:
        return None
    for part in raw.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name == SESSION_COOKIE and value.strip():
            return value.strip()
    return None


def presents_api_key(headers: Headers) -> bool:
    presented = presented_credential(headers)
    return (
        presented is not None
        and presented[0] == "bearer"
        and presented[1].startswith(API_KEY_PREFIX)
    )
