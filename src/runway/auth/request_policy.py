"""Rules that depend on who is asking and which route they hit.

Called by `get_principal` on every route, after the principal is resolved:

1. API keys work on `/api/v1` (and the MCP server). Unversioned routes check
   no scopes, so a key is refused there; local clients call them without a
   key as before (invariant 8).
2. `RUNWAY_LOCAL_REQUIRE_KEY_FOR_WRITES=true` (local profile): a write with no
   key is refused unless its route is in `LEGACY_WRITE_EXEMPTIONS`, which lists
   every pre-v1 write route so Muse and the web app keep working (spec 06
   section 9). Reads never need a key.
3. Release routes (any `POST` whose path ends in `/release`) go through
   `authorize_release`: TikTok by a key is 403 `human_required`, a missing
   `posts:release` is 403 `scope_missing`, and keys need the workspace policy
   and a grant. The publishing engine's route calls it too; this guard makes
   the rule hold for every release route, whoever builds it.

The daily caps on created posts and releases are enforced inside the mutation
transactions themselves (`runway.posts.service`), counted on the domain rows;
this guard is an early, pre-handler check only.
"""

from __future__ import annotations

from fastapi import Request
from sqlalchemy import select

from runway.auth.audit import AUDIT_DETAIL_STATE, AUDIT_TARGET_STATE, MUTATING_METHODS
from runway.auth.principal import LOCAL_OWNER_ID, Principal
from runway.auth.problems import Problem, ProblemCode
from runway.auth.release_policy import authorize_release
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import DraftTarget

V1_PREFIX = "/api/v1"
# Read-only documents a key holder may fetch outside /api/v1.
KEY_READABLE_LEGACY_ROUTES = frozenset({"/openapi.json", "/docs", "/redoc"})
# Path parameters that name a draft target on release routes.
TARGET_PATH_PARAMETERS = ("tid", "target_id")
_POLICY_DONE_STATE = "runway_request_policy_done"

# Every write route that existed before /api/v1 (2026-11-22). With
# RUNWAY_LOCAL_REQUIRE_KEY_FOR_WRITES=true these still accept a request with no
# key, because Muse's use of them is unknown (open question Q2). A test fails
# when an unversioned write route is added without a decision here.
LEGACY_WRITE_EXEMPTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/analysis/history"),
        ("PATCH", "/api/annotations/{post_id}"),
        ("POST", "/api/candidates/{candidate_id}/block-domain"),
        ("POST", "/api/candidates/{candidate_id}/captions"),
        ("POST", "/api/candidates/{candidate_id}/reject"),
        ("POST", "/api/capture/browser-checkpoint"),
        ("POST", "/api/capture/fixture"),
        ("PATCH", "/api/catalog/{post_id}/eligibility"),
        ("POST", "/api/content/groups"),
        ("POST", "/api/content/{content_id}/group"),
        ("POST", "/api/drafts"),
        ("DELETE", "/api/drafts/{draft_id}"),
        ("PATCH", "/api/drafts/{draft_id}"),
        ("POST", "/api/drafts/{draft_id}/targets/{target_id}/mark-published"),
        ("POST", "/api/editorial/options/ensure"),
        ("POST", "/api/editorial/proposals/{proposal_id}/approve"),
        ("POST", "/api/editorial/proposals/{proposal_id}/reject"),
        ("POST", "/api/editorial/proposals/{proposal_id}/skip-image"),
        ("POST", "/api/generation-runs"),
        ("PATCH", "/api/inbox/{comment_id}"),
        ("DELETE", "/api/inbox/{comment_id}/draft"),
        ("PUT", "/api/inbox/{comment_id}/draft"),
        ("POST", "/api/inbox/{comment_id}/reply"),
        ("POST", "/api/lineup/push"),
        ("PATCH", "/api/lineup/{proposal_id}"),
        ("POST", "/api/lineup/{proposal_id}/remove"),
        ("POST", "/api/lineup/{proposal_id}/retry"),
        ("POST", "/api/platforms/accounts/{account_id}/disconnect"),
        ("POST", "/api/platforms/accounts/{account_id}/reconnect"),
        ("POST", "/api/platforms/accounts/{account_id}/sync"),
        ("POST", "/api/platforms/instagram/import-token"),
        ("POST", "/api/platforms/{platform}/connect"),
        ("POST", "/api/profiles/build"),
        ("POST", "/api/profiles/evaluate"),
        ("PATCH", "/api/proposals/{proposal_id}"),
        ("POST", "/api/proposals/{proposal_id}/approve"),
        ("POST", "/api/proposals/{proposal_id}/block-domain"),
        ("POST", "/api/proposals/{proposal_id}/block-image"),
        ("POST", "/api/proposals/{proposal_id}/feedback"),
        ("POST", "/api/proposals/{proposal_id}/internal-schedule"),
        ("POST", "/api/proposals/{proposal_id}/metadata"),
        ("POST", "/api/proposals/{proposal_id}/regenerate"),
        ("POST", "/api/proposals/{proposal_id}/reject"),
        ("POST", "/api/proposals/{proposal_id}/replace"),
        ("POST", "/api/proposals/{proposal_id}/reschedule"),
        ("POST", "/api/proposals/{proposal_id}/rights-review"),
        ("POST", "/api/proposals/{proposal_id}/select-alternative"),
        ("POST", "/api/proposals/{proposal_id}/youtube/prepare"),
        ("POST", "/api/proposals/{proposal_id}/youtube/verify"),
        ("POST", "/api/publisher/attempts/{attempt_id}/confirm"),
        ("POST", "/api/publisher/connectors/validate"),
        ("POST", "/api/publisher/queue/resume"),
        ("POST", "/api/publisher/session/validate"),
        ("POST", "/api/search-runs"),
        ("PATCH", "/api/settings/full"),
    }
)


def route_template(request: Request) -> str:
    route = request.scope.get("route")
    return str(getattr(route, "path", None) or request.url.path)


def is_v1_path(path: str) -> bool:
    return path == V1_PREFIX or path.startswith(V1_PREFIX + "/")


def is_release_route(method: str, template: str) -> bool:
    return method == "POST" and template.rstrip("/").endswith("/release")


def _database(request: Request) -> Database | None:
    database = getattr(request.app.state, "database", None)
    return database if isinstance(database, Database) else None


def _is_local_owner_trust(settings: Settings, principal: Principal) -> bool:
    return (
        settings.profile == "local"
        and principal.kind == "user"
        and (principal.id == LOCAL_OWNER_ID)
    )


def _target_id(request: Request) -> int | None:
    for name in TARGET_PATH_PARAMETERS:
        value = request.path_params.get(name)
        if value is not None and str(value).isdigit():
            return int(value)
    return None


def _guard_release(request: Request, principal: Principal) -> None:
    database = _database(request)
    if database is None:
        # Without the database neither the target nor the policy is known.
        raise Problem.of(ProblemCode.SCOPE_MISSING, "release cannot be authorized here")
    target_id = _target_id(request)
    with database.session() as session:
        platform: str | None = None
        if target_id is not None:
            platform = session.scalar(
                select(DraftTarget.platform).where(
                    DraftTarget.id == target_id,
                    DraftTarget.workspace_id == principal.workspace_id,
                )
            )
        authorize_release(session, principal, platform=platform)
    if target_id is not None:
        setattr(request.state, AUDIT_TARGET_STATE, ("target", target_id))
    if platform is not None:
        setattr(request.state, AUDIT_DETAIL_STATE, {"platform": platform})


def enforce_request_policy(settings: Settings, request: Request, principal: Principal) -> None:
    if getattr(request.state, _POLICY_DONE_STATE, False):
        return
    method = request.method.upper()
    template = route_template(request)
    path = request.url.path
    key_readable = method in {"GET", "HEAD"} and template in KEY_READABLE_LEGACY_ROUTES
    if principal.kind == "agent" and not is_v1_path(path) and not key_readable:
        raise Problem.of(
            ProblemCode.SCOPE_MISSING,
            "API keys work on /api/v1 and the MCP server only; unversioned routes "
            "check no scopes, so they refuse keys",
        )
    if (
        settings.local_require_key_for_writes
        and method in MUTATING_METHODS
        and _is_local_owner_trust(settings, principal)
        and (method, template) not in LEGACY_WRITE_EXEMPTIONS
    ):
        raise Problem.of(
            ProblemCode.AUTHENTICATION_REQUIRED,
            "this Runway requires an API key for this write "
            "(RUNWAY_LOCAL_REQUIRE_KEY_FOR_WRITES=true); send Authorization: Bearer rwy_...",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if is_v1_path(path) and is_release_route(method, template):
        _guard_release(request, principal)
    setattr(request.state, _POLICY_DONE_STATE, True)
