"""The "Needs your approval" queue backend (spec 06 section 4).

Listing: targets in state `ready` on posts created by agents. With the engine
connected, `AgentServices.posts.list_awaiting_release` answers. Before that,
`guarded_awaiting_release` reads `draft_targets` and `content_drafts`
directly, using the engine's columns (`draft_targets.state`,
`content_drafts.source`, ...) only when they exist; without them it returns
an empty list.

Rejection: a person rejects a target with a reason. The engine hook returns
the target to `draft`; this module records `target.rejected` with the reason
and the person's edits as structured before/after data (the learning signal
Muse needs, spec 06 section 8).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, Field
from sqlalchemy import select

from runway.agent.services import AgentServices, ApprovalItem, ApprovalPage
from runway.api.v1.pagination import decode_cursor, encode_cursor
from runway.auth.audit import record_audit
from runway.auth.principal import POSTS_RELEASE, Principal, require, require_person
from runway.auth.problems import Problem, ProblemCode
from runway.db.base import Database
from runway.db.models import ContentDraft, DraftTarget
from runway.events.log import EventPayloadTooLarge, record_event
from runway.events.types import TARGET_REJECTED

APPROVALS_CURSOR_SCOPE = "approvals"
# The engine's columns this fallback needs; without them nothing can be
# "ready and created by an agent" yet.
REQUIRED_TARGET_COLUMNS = frozenset({"id", "draft_id", "workspace_id", "platform", "state"})
REQUIRED_POST_COLUMNS = frozenset({"id", "title", "source"})
OPTIONAL_TARGET_COLUMNS = ("account_id", "post_type", "planned_at", "payload_hash", "version")
OPTIONAL_POST_COLUMNS = ("created_by", "ai_generated")
READY_STATE = "ready"
AGENT_SOURCE = "agent"


class FieldEdit(BaseModel):
    """One change a person made before deciding: which field, from what, to what."""

    field: str = Field(min_length=1, max_length=80)
    before: Any = None
    after: Any = None


class RejectRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    edits: list[FieldEdit] = Field(default_factory=list, max_length=20)


def _columns(connection: sa.Connection, table: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(connection).get_columns(table)}


def _as_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def guarded_awaiting_release(
    database: Database, workspace_id: int, cursor: str | None, limit: int
) -> ApprovalPage:
    """Agent-created `ready` targets, read straight from the tables (engine not connected)."""
    with database.session() as session:
        connection = session.connection()
        target_columns = _columns(connection, "draft_targets")
        post_columns = _columns(connection, "content_drafts")
        if not (
            target_columns >= REQUIRED_TARGET_COLUMNS and post_columns >= REQUIRED_POST_COLUMNS
        ):
            return ApprovalPage(items=[], next_cursor=None)
        target_names = sorted(
            REQUIRED_TARGET_COLUMNS | {c for c in OPTIONAL_TARGET_COLUMNS if c in target_columns}
        )
        post_names = sorted(
            REQUIRED_POST_COLUMNS | {c for c in OPTIONAL_POST_COLUMNS if c in post_columns}
        )
        targets = sa.table("draft_targets", *(sa.column(name) for name in target_names))
        posts = sa.table("content_drafts", *(sa.column(name) for name in post_names))
        statement = (
            select(
                *(targets.c[name].label(f"t_{name}") for name in target_names),
                *(posts.c[name].label(f"p_{name}") for name in post_names),
            )
            .select_from(targets.join(posts, targets.c.draft_id == posts.c.id))
            .where(
                targets.c.workspace_id == workspace_id,
                targets.c.state == READY_STATE,
                posts.c.source == AGENT_SOURCE,
            )
        )
        if cursor is not None:
            position = decode_cursor(cursor, scope=APPROVALS_CURSOR_SCOPE, descending=True)
            statement = statement.where(targets.c.id < position)
        rows = (
            session.execute(statement.order_by(targets.c.id.desc()).limit(limit + 1))
            .mappings()
            .all()
        )
    items = [
        ApprovalItem(
            target_id=int(row["t_id"]),
            post_id=int(row["t_draft_id"]),
            platform=str(row["t_platform"]),
            account_id=row.get("t_account_id"),
            post_type=row.get("t_post_type"),
            title=str(row["p_title"]),
            planned_at=_as_datetime(row.get("t_planned_at")),
            payload_hash=row.get("t_payload_hash"),
            version=row.get("t_version"),
            source=row.get("p_source"),
            created_by=row.get("p_created_by"),
            ai_generated=(
                bool(row["p_ai_generated"]) if row.get("p_ai_generated") is not None else None
            ),
        )
        for row in rows[:limit]
    ]
    next_cursor = (
        encode_cursor(items[-1].target_id, scope=APPROVALS_CURSOR_SCOPE, descending=True)
        if len(rows) > limit
        else None
    )
    return ApprovalPage(items=items, next_cursor=next_cursor)


def list_awaiting_release(
    database: Database,
    services: AgentServices,
    workspace_id: int,
    cursor: str | None,
    limit: int,
) -> ApprovalPage:
    """The approval queue: the engine's function when connected, else the guarded read."""
    if services.connected:
        return services.posts.list_awaiting_release(workspace_id, cursor, limit)
    return guarded_awaiting_release(database, workspace_id, cursor, limit)


def _edits_payload(edits: Sequence[FieldEdit]) -> list[dict[str, Any]]:
    return [{"field": edit.field, "before": edit.before, "after": edit.after} for edit in edits]


def reject_target(
    database: Database,
    services: AgentServices,
    principal: Principal,
    target_id: int,
    request: RejectRequest,
) -> dict[str, object]:
    """A person rejects a target: engine hook, then `target.rejected` and an audit row."""
    require(principal, POSTS_RELEASE)
    # Rejecting is a person's judgement call; a key with posts:release may
    # release its granted platforms but can never send a target back.
    require_person(principal, "reject a target")
    with database.session() as session:
        target = session.scalar(
            select(DraftTarget).where(
                DraftTarget.id == target_id, DraftTarget.workspace_id == principal.workspace_id
            )
        )
        if target is None:
            raise Problem.of(ProblemCode.NOT_FOUND, "no such target in this workspace")
        post = session.get(ContentDraft, target.draft_id)
        post_id = target.draft_id
        platform = target.platform
        post_title = post.title if post is not None else None
    services.posts.reject_target(principal, target_id, request.reason)
    payload: dict[str, Any] = {
        "target_id": target_id,
        "post_id": post_id,
        "platform": platform,
        "reason": request.reason,
        "edits": _edits_payload(request.edits),
        "rejected_by": f"{principal.kind}:{principal.id}",
    }
    try:
        with database.session() as session:
            event = record_event(
                session, principal.workspace_id, TARGET_REJECTED, "target", target_id, payload
            )
            record_audit(
                session,
                principal,
                "target.reject",
                target_type="target",
                target_id=target_id,
                detail={"platform": platform, "edit_fields": [e.field for e in request.edits]},
            )
            event_id = event.id
    except EventPayloadTooLarge as exc:
        raise Problem.of(
            ProblemCode.VALIDATION_FAILED,
            "the reason and edits are too large for one event; shorten them",
        ) from exc
    return {
        "target_id": target_id,
        "post_id": post_id,
        "platform": platform,
        "title": post_title,
        "rejected": True,
        "event_id": event_id,
        "reason": request.reason,
        "edits": payload["edits"],
    }
