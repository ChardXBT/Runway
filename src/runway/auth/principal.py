"""Who is acting, and what they may do (spec 01 section 3).

Every API route depends on `get_principal`; authorization is `require`. No
route checks roles directly.
"""

from __future__ import annotations

import logging
import time
from typing import Literal

from fastapi import Request
from pydantic import BaseModel, ConfigDict

from runway.auth.problems import Problem, ProblemCode
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import Workspace, utcnow

logger = logging.getLogger(__name__)

PrincipalKind = Literal["user", "agent", "system"]
Role = Literal["owner", "editor", "approver", "viewer"]

ACCOUNTS_READ = "accounts:read"
ACCOUNTS_MANAGE = "accounts:manage"
CONTENT_READ = "content:read"
INBOX_READ = "inbox:read"
INBOX_DRAFT = "inbox:draft"
INBOX_REPLY = "inbox:reply"
MEDIA_WRITE = "media:write"
POSTS_READ = "posts:read"
POSTS_WRITE = "posts:write"
POSTS_SCHEDULE = "posts:schedule"
POSTS_RELEASE = "posts:release"
WORKSPACE_ADMIN = "workspace:admin"

ALL_SCOPES: frozenset[str] = frozenset(
    {
        ACCOUNTS_READ,
        ACCOUNTS_MANAGE,
        CONTENT_READ,
        INBOX_READ,
        INBOX_DRAFT,
        INBOX_REPLY,
        MEDIA_WRITE,
        POSTS_READ,
        POSTS_WRITE,
        POSTS_SCHEDULE,
        POSTS_RELEASE,
        WORKSPACE_ADMIN,
    }
)

_VIEWER: frozenset[str] = frozenset(scope for scope in ALL_SCOPES if scope.endswith(":read"))
_EDITOR: frozenset[str] = _VIEWER | {INBOX_DRAFT, MEDIA_WRITE, POSTS_WRITE, POSTS_SCHEDULE}
_APPROVER: frozenset[str] = _EDITOR | {POSTS_RELEASE, INBOX_REPLY}

ROLE_SCOPES: dict[Role, frozenset[str]] = {
    "viewer": _VIEWER,
    "editor": _EDITOR,
    "approver": _APPROVER,
    "owner": ALL_SCOPES,
}

LOCAL_WORKSPACE_ID = 1
LOCAL_OWNER_ID = "local-owner"
# Agent keys use this bearer prefix (spec 06 section 2). Key resolution arrives
# with the api_keys work; until then a presented key is refused, never mapped
# to the owner.
API_KEY_PREFIX = "rwy_"


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: PrincipalKind
    id: str
    workspace_id: int
    role: Role | None
    scopes: frozenset[str]
    display: str


def scopes_for_role(role: Role) -> frozenset[str]:
    return ROLE_SCOPES[role]


def local_owner(display: str = "Local owner") -> Principal:
    return Principal(
        kind="user",
        id=LOCAL_OWNER_ID,
        workspace_id=LOCAL_WORKSPACE_ID,
        role="owner",
        scopes=ALL_SCOPES,
        display=display,
    )


def system_worker(workspace_id: int = LOCAL_WORKSPACE_ID) -> Principal:
    """The background worker acting for one workspace."""
    return Principal(
        kind="system",
        id="worker",
        workspace_id=workspace_id,
        role=None,
        scopes=ALL_SCOPES,
        display="Runway worker",
    )


def require(principal: Principal, scope: str) -> None:
    """The only authorization check. Raises a 403 `scope_missing` problem."""
    if scope not in ALL_SCOPES:
        raise ValueError(f"unknown scope {scope!r}")
    if scope not in principal.scopes:
        raise Problem.of(ProblemCode.SCOPE_MISSING, f"this action needs the {scope} scope")


def _presented_api_key(request: Request) -> bool:
    authorization = request.headers.get("authorization", "")
    scheme, _, credential = authorization.partition(" ")
    return scheme.casefold() == "bearer" and credential.strip().startswith(API_KEY_PREFIX)


def resolve_principal(settings: Settings, request: Request) -> Principal:
    if _presented_api_key(request):
        raise Problem.of(
            ProblemCode.API_KEY_UNSUPPORTED,
            "this Runway version cannot verify API keys; send the request without one",
        )
    if settings.profile == "local":
        return local_owner()
    # Hosted: the identity provider seam is a later job, so nobody is authenticated.
    raise Problem.of(
        ProblemCode.AUTHENTICATION_REQUIRED,
        "sign in to use this Runway workspace",
        headers={"WWW-Authenticate": "Bearer"},
    )


# workspaces.last_seen_at is the "a human was here" heartbeat the adaptive
# sync scheduler reads for its 14-day inactivity rule (spec 04 section 3).
# One write per workspace per 10 minutes is plenty for a day-scale rule.
SEEN_WRITE_INTERVAL_SECONDS = 600.0


def _record_workspace_seen(request: Request, principal: Principal) -> None:
    """Touch `workspaces.last_seen_at` for human requests, throttled per app.

    Never raises: a broken heartbeat must not break the request it rode in on.
    The throttle map lives on `app.state` so each app keeps its own cadence.
    """
    if principal.kind != "user":
        return
    database = getattr(request.app.state, "database", None)
    if not isinstance(database, Database):
        return
    writes = getattr(request.app.state, "workspace_seen_writes", None)
    if writes is None:
        writes = {}
        request.app.state.workspace_seen_writes = writes
    moment = time.monotonic()
    if moment - writes.get(principal.workspace_id, -SEEN_WRITE_INTERVAL_SECONDS) < (
        SEEN_WRITE_INTERVAL_SECONDS
    ):
        return
    writes[principal.workspace_id] = moment
    try:
        with database.session() as session:
            workspace = session.get(Workspace, principal.workspace_id)
            if workspace is not None:
                workspace.last_seen_at = utcnow()
    except Exception:  # noqa: BLE001
        logger.exception("could not record workspace %s activity", principal.workspace_id)


def get_principal(request: Request) -> Principal:
    """FastAPI dependency required by every API route (spec 01 invariant 12)."""
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        # Fail closed: without the app's settings the profile is unknown.
        raise RuntimeError("the application has no settings; build it with create_app()")
    principal = resolve_principal(settings, request)
    request.state.principal = principal
    _record_workspace_seen(request, principal)
    return principal
