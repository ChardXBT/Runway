"""The "Needs your approval" queue (spec 06 section 4).

- `GET /api/v1/approvals`: targets in `ready` on posts an agent created,
  newest first, cursor-paginated (`posts:read`).
- `POST /api/v1/approvals/{target_id}/reject`: a person sends an agent's
  target back with a reason and, optionally, the edits they would have made
  (`posts:release`). Recorded as a `target.rejected` event.

Releasing is the publishing engine's release route; `runway.auth.request_policy`
guards every `/release` route (scope, `agent_release` policy, TikTok
`human_required`).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from runway.agent.approvals import RejectRequest, list_awaiting_release, reject_target
from runway.agent.services import ApprovalPage, build_agent_services
from runway.api.v1.idempotency import idempotent
from runway.api.v1.pagination import CursorParams, cursor_params
from runway.api.v1.router import build_v1_router
from runway.auth.principal import POSTS_READ, Principal, get_principal, require
from runway.config import Settings
from runway.db.base import Database


def build_approvals_router(database: Database, settings: Settings) -> APIRouter:
    router = build_v1_router(tags=["approvals"], prefix="/approvals")

    @router.get("", response_model=ApprovalPage)
    def list_approvals(
        principal: Principal = Depends(get_principal),
        params: CursorParams = Depends(cursor_params),
    ) -> ApprovalPage:
        """Agent-prepared targets waiting for a person to release or reject them."""
        require(principal, POSTS_READ)
        services = build_agent_services(database, settings)
        return list_awaiting_release(
            database, services, principal.workspace_id, params.cursor, params.limit
        )

    @router.post("/{target_id}/reject")
    @idempotent
    def reject(
        target_id: int,
        payload: RejectRequest,
        principal: Principal = Depends(get_principal),
    ) -> dict[str, Any]:
        """Reject a target with a reason; the agent learns it from `target.rejected`."""
        services = build_agent_services(database, settings)
        return reject_target(database, services, principal, target_id, payload)

    return router
