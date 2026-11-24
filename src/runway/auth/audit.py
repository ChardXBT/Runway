"""Audit rows for mutations (spec 06 section 5: every mutation by a key is recorded).

Two writers share the `audit_log` table:

- `record_audit` adds a row inside the caller's transaction. Services that own
  a transaction (key management, policy, approvals, MCP tools) use it.
- `AgentAuditMiddleware` adds one row after every successful HTTP mutation
  made by an API key, on any route, with the key id and name. It needs no
  cooperation from the route, so routes added by other jobs are covered too.

Rows hold ids, route templates and Runway's own summaries only; never request
bodies, platform text or secrets.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

import anyio
from sqlalchemy.orm import Session
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from runway.auth.principal import Principal
from runway.db.base import Database
from runway.db.models import AuditLogEntry, utcnow

logger = logging.getLogger(__name__)

MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Set by request policy on `request.state` to enrich the middleware's row.
AUDIT_TARGET_STATE = "runway_audit_target"
AUDIT_DETAIL_STATE = "runway_audit_detail"
MCP_ACTION_PREFIX = "mcp:"


def canonical_detail(detail: Mapping[str, Any] | None) -> str:
    """Stable JSON so `LIKE` lookups on fragments such as `"platform":"youtube"` work."""
    return json.dumps(dict(detail or {}), sort_keys=True, separators=(",", ":"), default=str)


def detail_fragment(key: str, value: str) -> str:
    """The exact text `canonical_detail` writes for one string member."""
    return json.dumps({key: value}, separators=(",", ":"))[1:-1]


def record_audit(
    session: Session,
    principal: Principal,
    action: str,
    *,
    target_type: str | None = None,
    target_id: str | int | None = None,
    detail: Mapping[str, Any] | None = None,
) -> AuditLogEntry:
    """Add an audit row to the caller's transaction (flushed, not committed)."""
    payload = dict(detail or {})
    if principal.kind == "agent":
        payload.setdefault("key_id", principal.id)
        payload.setdefault("key_name", principal.display)
    row = AuditLogEntry(
        workspace_id=principal.workspace_id,
        actor_kind=principal.kind,
        actor_id=principal.id,
        action=action[:80],
        target_type=target_type,
        target_id=None if target_id is None else str(target_id),
        detail_json=canonical_detail(payload),
        created_at=utcnow(),
    )
    session.add(row)
    session.flush()
    return row


def mcp_action(tool: str) -> str:
    return f"{MCP_ACTION_PREFIX}{tool}"


def http_action(method: str, route_path: str) -> str:
    return f"{method.upper()} {route_path}"


class AgentAuditMiddleware:
    """Writes an audit row after each successful mutation an API key makes.

    The principal is read from the request state that `get_principal` fills;
    the route template from the scope FastAPI matched. Replayed idempotent
    responses changed nothing and are not recorded again.
    """

    def __init__(self, app: ASGIApp, *, database: Database) -> None:
        self.app = app
        self.database = database

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in MUTATING_METHODS:
            await self.app(scope, receive, send)
            return
        outcome: dict[str, Any] = {}

        async def capture(message: Message) -> None:
            if message["type"] == "http.response.start":
                outcome["status"] = int(message["status"])
                headers = dict(message.get("headers") or [])
                outcome["replayed"] = headers.get(b"idempotent-replayed") == b"true"
            await send(message)

        await self.app(scope, receive, capture)
        status = outcome.get("status", 500)
        if not 200 <= status < 300 or outcome.get("replayed"):
            return
        state = scope.get("state") or {}
        principal = state.get("principal")
        if not isinstance(principal, Principal) or principal.kind != "agent":
            return
        route = scope.get("route")
        route_path = str(getattr(route, "path", None) or scope.get("path", ""))
        target = state.get(AUDIT_TARGET_STATE)
        extra = state.get(AUDIT_DETAIL_STATE)
        detail: dict[str, Any] = {"status": status, "path": str(scope.get("path", ""))}
        if isinstance(extra, Mapping):
            detail.update(extra)
        target_type, target_id = target if isinstance(target, tuple) else (None, None)

        def write() -> None:
            with self.database.session() as session:
                record_audit(
                    session,
                    principal,
                    http_action(str(scope["method"]), route_path),
                    target_type=target_type,
                    target_id=target_id,
                    detail=detail,
                )

        try:
            await anyio.to_thread.run_sync(write)
        except Exception:  # noqa: BLE001
            # The mutation already committed; a lost audit row must not turn
            # a success into an error response. It is logged loudly instead.
            logger.exception("audit row for an API-key mutation could not be written")
