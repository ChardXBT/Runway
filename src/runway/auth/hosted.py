"""Hosted-profile principal resolution (spec 01 section 3, spec 05 section 2).

verify the session with the identity provider (a refused token is charged to
the client address's rate-limit bucket) -> the per-user rate limit -> mirror
the user and find the workspace of the active organization -> take the Runway
role from `workspace_members` -> the per-workspace rate limit.
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from starlette.requests import Request

from runway.auth.principal import Principal, scopes_for_role
from runway.auth.problems import Problem
from runway.config import Settings
from runway.db.base import Database
from runway.hosted.quotas import quotas_for
from runway.hosted.ratelimit import RateLimit, RateLimiter, client_key, enforce
from runway.identity.base import (
    ExternalIdentity,
    IdentityProvider,
    IdentityRejected,
    IdentityUnavailable,
)
from runway.identity.mirror import IdentityMirror, MembershipRefused, ResolvedMember


def unauthenticated() -> Problem:
    return Problem(
        401,
        "authentication_required",
        "Authentication required",
        "sign in to use this Runway workspace",
        headers={"WWW-Authenticate": "Bearer"},
    )


def verify_identity(provider: IdentityProvider, request: Request) -> ExternalIdentity:
    """Run the provider; turn its refusals into 401/503 problems."""
    try:
        return provider.verify(request)
    except IdentityRejected as exc:
        if exc.code == "authentication_required":
            raise unauthenticated() from None
        raise Problem(
            401,
            exc.code,
            "Invalid session",
            exc.detail,
            headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
        ) from None
    except IdentityUnavailable:
        raise Problem(
            503,
            "identity_unavailable",
            "Sign-in is temporarily unavailable",
            "the identity provider could not be reached; try again shortly",
            headers={"Retry-After": "30"},
        ) from None


def _resolve_member(
    database: Database, mirror: IdentityMirror, settings: Settings, identity: ExternalIdentity
) -> tuple[ResolvedMember, int | None]:
    """Return the membership and the workspace's API requests-per-minute quota."""
    # A concurrent first sign-in can race on the unique user or member rows;
    # the loser retries once and then finds the winner's rows.
    for attempt in range(2):
        refused: MembershipRefused | None = None
        try:
            with database.session() as session:
                try:
                    member = mirror.resolve(session, identity)
                except MembershipRefused as exc:
                    # Leave the block normally so the user mirror is committed.
                    refused = exc
                else:
                    quotas = quotas_for(session, settings, member.workspace_id)
                    return member, quotas.api_requests_per_minute
        except IntegrityError:
            if attempt:
                raise
            continue
        if refused is not None:
            raise Problem(403, refused.code, "No access to this workspace", refused.detail)
    raise RuntimeError("membership resolution did not finish")  # pragma: no cover


def resolve_hosted_principal(settings: Settings, request: Request) -> Principal:
    state = request.app.state
    provider: IdentityProvider | None = getattr(state, "identity_provider", None)
    database: Database | None = getattr(state, "database", None)
    if provider is None or database is None:
        # No identity provider configured: nobody can be authenticated.
        raise unauthenticated()
    limiter: RateLimiter | None = getattr(state, "rate_limiter", None)
    try:
        identity = verify_identity(provider, request)
    except Problem as refused:
        presented = refused.status_code == 401 and refused.code != "authentication_required"
        if limiter is not None and presented:
            # A presented token failed: spend the client address's bucket, so
            # forged or expired tokens are throttled like anonymous calls. (A
            # request with no credential already paid in ClientRateLimit.)
            address = request.client.host if request.client else None
            enforce(
                limiter,
                client_key(address),
                RateLimit(settings.rate_limit_ip_per_minute),
                "too many requests from this address; slow down",
            )
        raise
    if limiter is not None:
        # Keyed by the verified user, never by address: many users may share
        # one address (the web server, a NAT), and one user may move between them.
        enforce(
            limiter,
            f"user:{provider.name}:{identity.user_id}",
            RateLimit(settings.rate_limit_principal_per_minute),
            "too many requests from this account; slow down",
        )
    mirror = IdentityMirror(provider.name, default_role=settings.hosted_default_member_role)
    member, workspace_rpm = _resolve_member(database, mirror, settings, identity)
    principal = Principal(
        kind="user",
        id=str(member.user_id),
        workspace_id=member.workspace_id,
        role=member.role,
        scopes=scopes_for_role(member.role),
        display=member.display,
    )
    if limiter is not None and workspace_rpm is not None:
        enforce(
            limiter,
            f"workspace:{principal.workspace_id}",
            RateLimit(workspace_rpm),
            "this workspace has reached its API requests per minute",
        )
    return principal
