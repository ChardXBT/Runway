"""API keys and the workspace agent policy (spec 06 sections 3 and 4).

- `GET /api/v1/keys`, `POST /api/v1/keys`, `GET /api/v1/keys/{key_id}`,
  `DELETE /api/v1/keys/{key_id}` (revoke), `PUT /api/v1/keys/{key_id}/release-grants`.
- `GET /api/v1/policy`, `PUT /api/v1/policy` (`agent_release`).

Everything here needs `workspace:admin`. Changing `agent_release` and release
grants is for people only: an API key, even one holding `workspace:admin`,
gets 403 `human_required`, so no key can widen its own release rights.

`POST /api/v1/keys` is deliberately not idempotent: a replay would have to
store the secret, and Runway keeps only its hash.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from fastapi import APIRouter, Depends, status
from pydantic import BaseModel, Field

from runway.api.v1.idempotency import idempotent
from runway.api.v1.pagination import CursorPage, CursorParams, cursor_params
from runway.api.v1.router import build_v1_router
from runway.auth.api_keys import (
    DEFAULT_AGENT_SCOPES,
    ApiKeyService,
    ApiKeyView,
    CreatedApiKey,
)
from runway.auth.principal import WORKSPACE_ADMIN, Principal, get_principal, require
from runway.auth.problems import Problem, ProblemCode
from runway.auth.release_policy import (
    HUMAN_ONLY_PLATFORMS,
    ReleaseGrantInput,
    policy_view,
    set_agent_release_policy,
)
from runway.config import Settings
from runway.db.base import Database


class CreateKeyRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    # Omitted: the default agent scopes (spec 06 section 3).
    scopes: list[str] | None = Field(default=None, max_length=20)
    expires_at: datetime | None = None
    release_grants: list[ReleaseGrantInput] = Field(default_factory=list, max_length=3)


class ReleaseGrantsRequest(BaseModel):
    grants: list[ReleaseGrantInput] = Field(default_factory=list, max_length=3)


class PolicyRequest(BaseModel):
    agent_release: Literal["never", "per_key"]


class PolicyResponse(BaseModel):
    agent_release: Literal["never", "per_key"]
    updated_by: str | None
    updated_at: datetime | None
    human_only_platforms: list[str]
    default_agent_scopes: list[str]


def require_person(principal: Principal, action: str) -> None:
    """Some decisions belong to a person, never to an API key."""
    if principal.kind == "agent":
        raise Problem.of(ProblemCode.HUMAN_REQUIRED, f"only a person can {action}")


def _policy_response(database: Database, workspace_id: int) -> PolicyResponse:
    with database.session() as session:
        view = policy_view(session, workspace_id)
    return PolicyResponse(
        agent_release=view.agent_release,
        updated_by=view.updated_by,
        updated_at=view.updated_at,
        human_only_platforms=sorted(HUMAN_ONLY_PLATFORMS),
        default_agent_scopes=sorted(DEFAULT_AGENT_SCOPES),
    )


def build_keys_router(database: Database, settings: Settings) -> APIRouter:
    router = build_v1_router(tags=["keys"])
    service = ApiKeyService(database, settings)

    @router.get("/keys", response_model=CursorPage[ApiKeyView])
    def list_keys(
        principal: Principal = Depends(get_principal),
        params: CursorParams = Depends(cursor_params),
    ) -> CursorPage[ApiKeyView]:
        """The workspace's keys, newest first. Secrets are never shown again."""
        require(principal, WORKSPACE_ADMIN)
        return service.list(principal.workspace_id, params)

    @router.post("/keys", status_code=status.HTTP_201_CREATED, response_model=CreatedApiKey)
    def create_key(
        payload: CreateKeyRequest, principal: Principal = Depends(get_principal)
    ) -> CreatedApiKey:
        """Create a key. `secret` is in this response only; store it now."""
        if payload.release_grants:
            require_person(principal, "grant posts:release to a key")
        return service.create(
            principal,
            name=payload.name,
            scopes=payload.scopes,
            expires_at=payload.expires_at,
            release_grants=payload.release_grants,
        )

    @router.get("/keys/{key_id}", response_model=ApiKeyView)
    def get_key(key_id: int, principal: Principal = Depends(get_principal)) -> ApiKeyView:
        require(principal, WORKSPACE_ADMIN)
        return service.get(principal.workspace_id, key_id)

    @router.delete("/keys/{key_id}", response_model=ApiKeyView)
    @idempotent
    def revoke_key(key_id: int, principal: Principal = Depends(get_principal)) -> ApiKeyView:
        """Revoke a key at once. Revoking twice is harmless."""
        return service.revoke(principal, key_id)

    @router.put("/keys/{key_id}/release-grants", response_model=ApiKeyView)
    @idempotent
    def set_release_grants(
        key_id: int,
        payload: ReleaseGrantsRequest,
        principal: Principal = Depends(get_principal),
    ) -> ApiKeyView:
        """Replace a key's per-platform release grants (needs `agent_release=per_key`).

        An empty list takes `posts:release` away. TikTok can never be granted.
        """
        require(principal, WORKSPACE_ADMIN)
        require_person(principal, "change a key's release grants")
        return service.set_release_grants(principal, key_id, payload.grants)

    @router.get("/policy", response_model=PolicyResponse)
    def get_policy(principal: Principal = Depends(get_principal)) -> PolicyResponse:
        require(principal, WORKSPACE_ADMIN)
        return _policy_response(database, principal.workspace_id)

    @router.put("/policy", response_model=PolicyResponse)
    @idempotent
    def put_policy(
        payload: PolicyRequest, principal: Principal = Depends(get_principal)
    ) -> PolicyResponse:
        """Set `agent_release`. `never` switches every release grant off at once."""
        require(principal, WORKSPACE_ADMIN)
        require_person(principal, "change the agent release policy")
        with database.session() as session:
            set_agent_release_policy(session, principal, payload.agent_release)
        return _policy_response(database, principal.workspace_id)

    return router
