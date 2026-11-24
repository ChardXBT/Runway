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
import re
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict
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
from runway.db.models import (
    DEFAULT_WORKSPACE_ID,
    AuditLogEntry,
    PublishJob,
    Workspace,
    utcnow,
)
from runway.jobs.queue import JobQueue, JobState
from runway.mediastore.errors import InvalidMediaKey, MediaStoreError
from runway.workspaces.lifecycle import (
    DELETE_WORKSPACE_JOB,
    EXPORT_WORKSPACE_JOB,
    delete_user,
    export_key,
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


class DeleteWorkspaceRequest(BaseModel):
    """The destructive-action echo: the workspace's slug or name, typed back."""

    model_config = ConfigDict(extra="forbid")

    confirm: str


_EXPORT_ID = re.compile(r"[0-9a-f]{16}")


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
        """The workspace's audit log, newest first; owners and approvers.

        The approver role has no workspace-admin scope (its grants stop at
        release and reply), so the read is gated on role; the admin scope
        stays sufficient so API keys keep the access they had.
        """
        if principal.role not in {"owner", "approver"} and WORKSPACE_ADMIN not in principal.scopes:
            raise _problem(ProblemCode.SCOPE_MISSING, "the audit log is for owners and approvers")
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
    def delete_workspace(
        payload: DeleteWorkspaceRequest,
        principal: Principal = Depends(get_principal),
    ) -> dict[str, object]:
        """Soft-delete the workspace; the purge job erases the rows.

        The caller types the workspace's slug or name back: deleting erases
        everything the workspace owns, so a bare click can never be enough.
        The local profile's only workspace cannot go at all - the install
        would be left with no workspace.
        """
        require_person(principal, "delete this workspace")
        require(principal, WORKSPACE_ADMIN)
        if principal.role != "owner":
            raise _problem(ProblemCode.SCOPE_MISSING, "only the workspace owner can delete it")

        with database.session() as session:
            workspace = current_workspace(session, principal)
            if payload.confirm not in {workspace.slug, workspace.name}:
                raise _problem(
                    ProblemCode.VALIDATION_FAILED,
                    "confirm must be the workspace's slug or name",
                )
            if settings.profile == "local" and workspace.id == DEFAULT_WORKSPACE_ID:
                raise _problem(
                    ProblemCode.INVALID_STATE,
                    "the local profile's only workspace cannot be deleted",
                )
            row = session.get(Workspace, workspace.id)
            if row is None:
                raise _problem(ProblemCode.NOT_FOUND, "no such workspace")
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
    def request_export(principal: Principal = Depends(get_principal)) -> dict[str, object]:
        """Enqueue a full workspace export; poll the download path for the file."""
        require_person(principal, "export this workspace")
        require(principal, WORKSPACE_ADMIN)
        # 16 lowercase hex chars: the export id doubles as the artifact key's
        # digest component, so the download finds the object without the
        # tracking job row (retention purges those after 14 days).
        export_id = secrets.token_hex(8)
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

    @router.get("/workspace/exports/{export_id}", response_model=None)
    def download_export(
        export_id: str,
        request: Request,
        principal: Principal = Depends(get_principal),
    ) -> FileResponse | RedirectResponse:
        """The finished export artifact; 409 while the job is still pending.

        The artifact is the durable record, not the publish_jobs row: in
        `hosted` it lives in object storage (API and worker share no disk),
        so a short-lived signed URL is handed out - this keeps working after
        the finished export job is retention-purged.
        """
        require(principal, WORKSPACE_ADMIN)
        # The id doubles as a file path (`local`) and an object key
        # (`hosted`); anything outside the issued 16-hex form can name
        # neither, so it is a 404 before `export_path` is ever built.
        if _EXPORT_ID.fullmatch(export_id) is None:
            raise _problem(ProblemCode.NOT_FOUND, "no such export")
        path = export_path(settings, principal.workspace_id, export_id)
        if path.is_file():
            return FileResponse(
                path,
                media_type="application/json",
                filename=f"runway-workspace-{principal.workspace_id}-export.json",
            )
        if settings.profile == "hosted":
            media_service = getattr(request.app.state, "media_service", None)
            store = media_service.stores.get("r2") if media_service is not None else None
            if store is not None:
                key = export_key(principal.workspace_id, export_id)
                try:
                    exists = store.object_size(key, workspace_id=principal.workspace_id)
                except InvalidMediaKey:
                    exists = None  # a URL id that cannot name an object
                except MediaStoreError as exc:
                    raise _problem(ProblemCode.CAPABILITY_NOT_READY, str(exc)) from exc
                if exists is not None:
                    url = store.signed_download_url(
                        key,
                        300,
                        workspace_id=principal.workspace_id,
                        filename=f"runway-workspace-{principal.workspace_id}-export.json",
                    )
                    if url is not None:
                        return RedirectResponse(url)
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
    def delete_me(principal: Principal = Depends(get_principal)) -> dict[str, object]:
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
