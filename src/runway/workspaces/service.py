"""The current workspace and its members (spec 01 sections 3-4).

Every function takes the workspace id from the caller's `Principal`, never
from the request, so one workspace's members cannot be read or changed from
another. Invitations and removals happen in the identity provider's own UI
and reach Runway through its webhooks (runway.identity.mirror).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runway.auth.principal import Principal, Role
from runway.auth.problems import Problem
from runway.db.models import AuditLogEntry, User, Workspace, WorkspaceMember


@dataclass(frozen=True, slots=True)
class WorkspaceSummary:
    id: int
    slug: str
    name: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemberSummary:
    user_id: int
    email: str | None
    display_name: str | None
    role: str
    joined_at: datetime


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def current_workspace(session: Session, principal: Principal) -> WorkspaceSummary:
    workspace = session.get(Workspace, principal.workspace_id)
    if workspace is None or workspace.deleted_at is not None:
        raise Problem(404, "workspace_not_found", "Workspace not found", "this workspace is gone")
    return WorkspaceSummary(
        id=workspace.id,
        slug=workspace.slug,
        name=workspace.name,
        created_at=_aware(workspace.created_at),
    )


def list_members(session: Session, principal: Principal) -> list[MemberSummary]:
    rows = session.execute(
        select(WorkspaceMember, User)
        .join(User, User.id == WorkspaceMember.user_id)
        .where(WorkspaceMember.workspace_id == principal.workspace_id)
        .order_by(WorkspaceMember.created_at, WorkspaceMember.id)
    ).all()
    return [
        MemberSummary(
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=member.role,
            joined_at=_aware(member.created_at),
        )
        for member, user in rows
    ]


def change_member_role(
    session: Session, principal: Principal, user_id: int, role: Role
) -> MemberSummary:
    """Set a member's Runway role. The caller must already hold `workspace:admin`."""
    member = session.scalar(
        select(WorkspaceMember).where(
            WorkspaceMember.workspace_id == principal.workspace_id,
            WorkspaceMember.user_id == user_id,
        )
    )
    user = session.get(User, user_id) if member is not None else None
    if member is None or user is None:
        raise Problem(
            404, "member_not_found", "Member not found", "no such member in this workspace"
        )
    if member.role == "owner" and role != "owner":
        # Serialize owner demotions per workspace: without the row lock, two
        # owners demoting each other under READ COMMITTED would both count two
        # owners and leave none. The lock is held until the request's
        # transaction ends, and the member and the count are read after it, so
        # a demotion committed meanwhile is seen. (SQLite ignores FOR UPDATE
        # and serializes writers itself.)
        session.execute(
            select(Workspace.id).where(Workspace.id == principal.workspace_id).with_for_update()
        )
        session.refresh(member)
    previous = member.role
    if previous == "owner" and role != "owner":
        owners = session.scalar(
            select(func.count(WorkspaceMember.id)).where(
                WorkspaceMember.workspace_id == principal.workspace_id,
                WorkspaceMember.role == "owner",
            )
        )
        if (owners or 0) <= 1:
            raise Problem(
                409,
                "last_owner",
                "The last owner cannot be demoted",
                "make another member an owner first",
            )
    if previous != role:
        member.role = role
        session.add(
            AuditLogEntry(
                workspace_id=principal.workspace_id,
                actor_kind=principal.kind,
                actor_id=principal.id,
                action="workspace.member_role_changed",
                target_type="user",
                target_id=str(user_id),
                detail_json=json.dumps({"from": previous, "to": role}, sort_keys=True),
            )
        )
        session.flush()
    return MemberSummary(
        user_id=user.id,
        email=user.email,
        display_name=user.display_name,
        role=member.role,
        joined_at=_aware(member.created_at),
    )
