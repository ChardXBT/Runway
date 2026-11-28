"""The factory every `/api/v1` router is built with (spec 06 section 6)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from enum import Enum

from fastapi import APIRouter, Depends
from fastapi.routing import APIRoute

from runway.api.v1.idempotency import RouteHandler, is_idempotent, wrap_idempotent_handler
from runway.auth.principal import Principal, get_principal
from runway.db.workspace import workspace_context

V1_PREFIX = "/api/v1"

_PROBLEM_RESPONSE: dict[str, object] = {
    "description": "An application/problem+json error with a stable `code`",
    "content": {"application/problem+json": {}},
}
V1_ERROR_RESPONSES: dict[int | str, dict[str, object]] = {
    status: _PROBLEM_RESPONSE for status in (401, 403, 404, 409, 422, 428, 429)
}


class V1Route(APIRoute):
    """APIRoute that honours `@idempotent` on mutation endpoints."""

    def get_route_handler(self) -> RouteHandler:
        handler = super().get_route_handler()
        if is_idempotent(self.endpoint):
            return wrap_idempotent_handler(handler)
        return handler


async def bind_workspace_context(
    principal: Principal = Depends(get_principal),
) -> AsyncIterator[None]:
    """Bind the request's database sessions to the principal's workspace.

    Async dependencies run in the request task, so the context set here is
    visible to the handler and to every session it opens - including work
    queued to worker threads, which inherit the task's context. `get_principal`
    itself is sync and runs in a threadpool, so it cannot set this itself.
    """
    with workspace_context(principal.workspace_id):
        yield


def build_v1_router(*, tags: Sequence[str | Enum], prefix: str = "") -> APIRouter:
    """A router under /api/v1 whose every route requires a Principal.

    `prefix` is relative to /api/v1 (for example "/posts"). Use it for every
    v1 router so the route-walk test and the problem+json handlers apply.
    """
    if prefix and (not prefix.startswith("/") or prefix.endswith("/")):
        raise ValueError("prefix must start with '/' and not end with '/'")
    return APIRouter(
        prefix=V1_PREFIX + prefix,
        tags=list(tags),
        dependencies=[Depends(get_principal), Depends(bind_workspace_context)],
        responses=V1_ERROR_RESPONSES,
        route_class=V1Route,
    )
