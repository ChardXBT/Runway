"""Compliance surface (spec 05 sections 4-5): delete, export and audit.

`DELETE /api/v1/workspace` soft-deletes the caller's workspace and enqueues
the purge job that detaches every grant and erases every row - the owner
asks, the worker does the irreversible part.

`POST /api/v1/workspace/export` enqueues an `export_workspace` job whose
artifact `GET /api/v1/workspace/exports/{export_id}` streams back once the
file exists. Artifacts live under the data dir per workspace.

`DELETE /api/v1/users/me` removes the caller's own rows; the last owner of
a workspace with other members must hand over ownership first.

`GET /api/v1/audit` pages the workspace's audit log, newest first.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select

from runway.api.v1.pagination import CursorParams, cursor_params, paginate
from runway.api.v1.router import build_v1_router
from runway.auth.audit import record_audit
from runway.auth.principal import (
    WORKSPACE_ADMIN,
    Principal,
    get_principal,
    require,
    require_person,
)
from runway.auth.problems import Problem, ProblemCode
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import AuditLogEntry, PublishJob, utcnow
from runway.jobs.queue import JobQueue, JobState
from runway.workspaces.lifecycle import (
    DELETE_WORKSPACE_JOB,
    EXPORT_WORKSPACE_JOB,
    delete_user,
    export_path,
)
from runway.workspaces.service import current_workspace


class AuditEntryView(BaseModel):
    id: int
    actor_kind: str
    actor_id: str
    action: str
    target_type: str | None
    target_id: str | None
    detail: dict[str, object]
    created_at: datetime


class AuditPage(BaseModel):
    items: list[AuditEntryView]
    next_cursor: str | None


def _problem(code: ProblemCode, detail: str) -> Problem:
    return Problem.of(code, detail)


def _audit_view(entry: AuditLogEntry) -> AuditEntryView:
    try:
        detail = json.loads(entry.detail_json or "{}")
    except json.JSONDecodeError:
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    return AuditEntryView(
        id=entry.id,
        actor_kind=entry.actor_kind,
        actor_id=entry.actor_id,
        action=entry.action,
        target_type=entry.target_type,
        target_id=entry.target_id,
        detail=detail,
        created_at=entry.created_at,
    )


def build_compliance_router(database: Database, settings: Settings) -> APIRouter:
    router = build_v1_router(tags=["compliance"], prefix="")
    queue = JobQueue(database)

    @router.get("/audit", response_model=AuditPage)
    def list_audit(
        principal: Principal = Depends(get_principal),
        params: CursorParams = Depends(cursor_params),
    ) -> AuditPage:
        """The workspace's audit log, newest first; admins only."""
        require(principal, WORKSPACE_ADMIN)
        with database.session() as session:
            rows, next_cursor = paginate(
                session,
                select(AuditLogEntry).where(AuditLogEntry.workspace_id == principal.workspace_id),
                AuditLogEntry.id,
                params,
                scope="audit",
            )
        return AuditPage(items=[_audit_view(row) for row in rows], next_cursor=next_cursor)

    @router.delete("/workspace", status_code=202)
    def delete_workspace(principal: Principal = Depends(get_principal)):
        """Soft-delete the workspace; the purge job erases the rows."""
        require_person(principal, "delete this workspace")
        require(principal, WORKSPACE_ADMIN)
        if principal.role != "owner":
            raise _problem(ProblemCode.SCOPE_MISSING, "only the workspace owner can delete it")
        from runway.db.models import Workspace

        with database.session() as session:
            workspace = current_workspace(session, principal)
            row = session.get(Workspace, workspace.id)
            row.deleted_at = utcnow()
            record_audit(
                session,
                principal,
                "workspace.delete",
                target_type="workspace",
                target_id=workspace.id,
                detail={"slug": workspace.slug},
            )
            queue.enqueue_pending_job(
                session,
                DELETE_WORKSPACE_JOB,
                idempotency_key=f"delete-workspace:{workspace.id}",
                payload={"workspace_id": workspace.id},
                workspace_id=workspace.id,
            )
        return {"status": "deleting", "workspace_id": workspace.id}

    @router.post("/workspace/export", status_code=202)
    def request_export(principal: Principal = Depends(get_principal)):
        """Enqueue a full workspace export; poll the download path for the file."""
        require_person(principal, "export this workspace")
        require(principal, WORKSPACE_ADMIN)
        export_id = secrets.token_urlsafe(12)
        with database.session() as session:
            current_workspace(session, principal)
            queue.enqueue_pending_job(
                session,
                EXPORT_WORKSPACE_JOB,
                idempotency_key=f"export:{export_id}",
                payload={
                    "workspace_id": principal.workspace_id,
                    "export_id": export_id,
                },
                workspace_id=principal.workspace_id,
            )
            record_audit(
                session,
                principal,
                "workspace.export",
                target_type="workspace",
                target_id=principal.workspace_id,
                detail={"export_id": export_id},
            )
        return {"export_id": export_id, "status": "pending"}

    @router.get("/workspace/exports/{export_id}")
    def download_export(
        export_id: str, principal: Principal = Depends(get_principal)
    ) -> FileResponse:
        """The finished export artifact; 409 while the job is still pending."""
        require(principal, WORKSPACE_ADMIN)
        path = export_path(settings, principal.workspace_id, export_id)
        if path.is_file():
            return FileResponse(
                path,
                media_type="application/json",
                filename=f"runway-workspace-{principal.workspace_id}-export.json",
            )
        with database.session() as session:
            job = session.scalar(
                select(PublishJob).where(
                    PublishJob.idempotency_key == f"export:{export_id}",
                    PublishJob.workspace_id == principal.workspace_id,
                )
            )
        if job is not None and job.state in (
            JobState.PENDING.value,
            JobState.RUNNING.value,
        ):
            raise _problem(ProblemCode.INVALID_STATE, "the export is still being built")
        raise _problem(ProblemCode.NOT_FOUND, "no such export")

    @router.delete("/users/me")
    def delete_me(principal: Principal = Depends(get_principal)):
        """Remove the caller's own rows in every workspace they belong to."""
        require_person(principal, "delete your account")
        try:
            user_id = int(principal.id)
        except (TypeError, ValueError):
            raise _problem(
                ProblemCode.VALIDATION_FAILED,
                "this identity has no self-service deletion",
            ) from None
        with database.session() as session:
            workspace_ids = delete_user(session, user_id)
            record_audit(
                session,
                principal,
                "user.delete",
                target_type="user",
                target_id=user_id,
                detail={"workspace_ids": workspace_ids},
            )
        return {"deleted": True, "workspace_ids": workspace_ids}

    return router
