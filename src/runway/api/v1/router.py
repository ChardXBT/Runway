"""The factory every `/api/v1` router is built with (spec 06 section 6)."""

from __future__ import annotations

from collections.abc import Sequence
from enum import Enum

from fastapi import APIRouter, Depends
from fastapi.routing import APIRoute

from runway.api.v1.idempotency import RouteHandler, is_idempotent, wrap_idempotent_handler
from runway.auth.principal import get_principal

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
        dependencies=[Depends(get_principal)],
        responses=V1_ERROR_RESPONSES,
        route_class=V1Route,
    )
