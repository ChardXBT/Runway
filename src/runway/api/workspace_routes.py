"""`/api/v1/workspace`: the caller's workspace, its members and their Runway roles.

The workspace is always the caller's own (`Principal.workspace_id`): in
`hosted` that is the identity provider's active organization, in `local`
workspace 1. Inviting and removing people is done in the identity provider's
organization UI (Clerk's `<OrganizationProfile />` or its dashboard); Runway
mirrors the result and owns only the roles, which are changed here.
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from runway.auth.principal import (
    CONTENT_READ,
    WORKSPACE_ADMIN,
    Principal,
    Role,
    get_principal,
    require,
)
from runway.config import Settings
from runway.db.base import Database
from runway.workspaces.service import (
    MemberSummary,
    change_member_role,
    current_workspace,
    list_members,
)


class WorkspaceView(BaseModel):
    id: int
    slug: str
    name: str
    profile: str
    created_at: datetime
    your_role: Role | None
    members_managed_by: str


class MemberView(BaseModel):
    user_id: int
    email: str | None
    display_name: str | None
    role: str
    joined_at: datetime


class MemberList(BaseModel):
    members: list[MemberView]


class RoleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: Role


def _member_view(member: MemberSummary) -> MemberView:
    return MemberView(
        user_id=member.user_id,
        email=member.email,
        display_name=member.display_name,
        role=member.role,
        joined_at=member.joined_at,
    )


def build_workspace_router(database: Database, settings: Settings) -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/workspace",
        tags=["workspace"],
        dependencies=[Depends(get_principal)],
    )
    managed_by = "identity_provider" if settings.profile == "hosted" else "local_owner"

    @router.get("", response_model=WorkspaceView)
    def workspace(principal: Principal = Depends(get_principal)) -> WorkspaceView:
        require(principal, CONTENT_READ)
        with database.session() as session:
            summary = current_workspace(session, principal)
        return WorkspaceView(
            id=summary.id,
            slug=summary.slug,
            name=summary.name,
            profile=settings.profile,
            created_at=summary.created_at,
            your_role=principal.role,
            members_managed_by=managed_by,
        )

    @router.get("/members", response_model=MemberList)
    def members(principal: Principal = Depends(get_principal)) -> MemberList:
        require(principal, CONTENT_READ)
        with database.session() as session:
            rows = list_members(session, principal)
        return MemberList(members=[_member_view(row) for row in rows])

    @router.patch("/members/{user_id}", response_model=MemberView)
    def change_role(
        user_id: int, change: RoleChange, principal: Principal = Depends(get_principal)
    ) -> MemberView:
        require(principal, WORKSPACE_ADMIN)
        with database.session() as session:
            member = change_member_role(session, principal, user_id, change.role)
        return _member_view(member)

    return router
