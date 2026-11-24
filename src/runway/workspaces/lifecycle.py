"""Workspace-level data duties (spec 05 sections 4-5).

`export_workspace_data` dumps every table row a workspace owns - discovered
from the mapped metadata, so a table added later is exported without code
here - plus its members and users. `purge_workspace` is the inverse: every
row keyed by the workspace goes, members and identity links included, and
then the workspace row itself. Account grants are detached by the caller
first: revoking needs the provider registry, which this module never sees.

`delete_user` removes one person's rows: memberships everywhere, their audit
entries keep the actor id (the audit record is the point of audit), and the
`users` row last so a dangling member row can never exist.
"""

from __future__ import annotations

import base64
import json
import logging
import shutil
from datetime import UTC, date, datetime
from enum import Enum
from typing import Any

from sqlalchemy import delete, func, inspect, select
from sqlalchemy.orm import Session

from runway.auth.problems import Problem, ProblemCode
from runway.db.base import Base
from runway.db.models import ConnectedAccount, User, Workspace, WorkspaceMember

logger = logging.getLogger(__name__)

# Tables whose rows belong to a person rather than the workspace, keyed by
# the users.id the API exposes as `Principal.id` in the hosted profile.
# `workspace_members` goes through the table-level delete below: without an
# ORM relationship between User and WorkspaceMember the unit of work cannot
# order the deletes, and the users row delete would hit the FK first.
_USER_TABLES = ("workspace_members",)


def _workspace_tables() -> list[Any]:
    """Mapped tables carrying a `workspace_id` column, children first.

    `Base.metadata.sorted_tables` is topologically ordered by foreign-key
    dependency, so deleting in reverse order never orphans a child row.
    """
    return [table for table in reversed(Base.metadata.sorted_tables) if "workspace_id" in table.c]


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return str(value)


def _row_dict(row: Any) -> dict[str, Any]:
    mapper = inspect(type(row))
    return {
        column.key: _json_value(getattr(row, column.key))
        for column in mapper.columns
        if column.key != "token_ciphertext"
    }


def export_workspace_data(session: Session, workspace_id: int) -> dict[str, Any]:
    """Every row the workspace owns, as a JSON-ready document.

    Token ciphertext is never exported: a grant is the platform's, and the
    ciphertext is useless without `RUNWAY_TOKEN_KEYS` anyway.
    """
    workspace = session.get(Workspace, workspace_id)
    document: dict[str, Any] = {
        "format": "runway-workspace-export",
        "format_version": 1,
        "exported_at": datetime.now(UTC).isoformat(),
        "workspace": _row_dict(workspace) if workspace is not None else None,
        "tables": {},
    }
    for table in _workspace_tables():
        rows = session.execute(
            table.select().where(table.c.workspace_id == workspace_id)
        ).mappings()
        document["tables"][table.name] = [
            {key: _json_value(value) for key, value in row.items()} for row in rows
        ]
    user_ids = [
        row[0]
        for row in session.execute(
            select(WorkspaceMember.user_id).where(WorkspaceMember.workspace_id == workspace_id)
        )
    ]
    if user_ids:
        users = session.scalars(select(User).where(User.id.in_(user_ids))).all()
        document["tables"]["users"] = [_row_dict(user) for user in users]
    return document


def purge_workspace(session: Session, workspace_id: int) -> dict[str, int]:
    """Delete every workspace-owned row, then the workspace. Returns counts."""
    counts: dict[str, int] = {}
    for table in _workspace_tables():
        result = session.execute(delete(table).where(table.c.workspace_id == workspace_id))
        counts[table.name] = result.rowcount or 0
    deleted = session.get(Workspace, workspace_id)
    if deleted is not None:
        session.delete(deleted)
    return counts


def delete_user(session: Session, user_id: int) -> list[int]:
    """Remove one person's rows. Returns the workspace ids they belonged to.

    Refuses while the user is the last owner of a workspace that still has
    other members - that workspace would be left with no owner.
    """
    memberships = session.scalars(
        select(WorkspaceMember).where(WorkspaceMember.user_id == user_id)
    ).all()
    blocking: list[int] = []
    for member in memberships:
        if member.role != "owner":
            continue
        owners = (
            session.scalar(
                select(func.count(WorkspaceMember.id)).where(
                    WorkspaceMember.workspace_id == member.workspace_id,
                    WorkspaceMember.role == "owner",
                )
            )
            or 0
        )
        others = (
            session.scalar(
                select(func.count(WorkspaceMember.id)).where(
                    WorkspaceMember.workspace_id == member.workspace_id,
                    WorkspaceMember.user_id != user_id,
                )
            )
            or 0
        )
        if owners <= 1 and others > 0:
            blocking.append(int(member.workspace_id))
    if blocking:
        raise Problem.of(
            ProblemCode.INVALID_STATE,
            "you are the last owner of a workspace with other members; "
            "transfer ownership or delete the workspace first",
        )
    workspace_ids = sorted({int(member.workspace_id) for member in memberships})
    for table_name in _USER_TABLES:
        table = Base.metadata.tables.get(table_name)
        if table is not None and "user_id" in table.c:
            session.execute(delete(table).where(table.c.user_id == user_id))
    session.flush()
    user = session.get(User, user_id)
    if user is not None:
        session.delete(user)
    return workspace_ids


# ------------------------------------------------------------------- jobs

EXPORT_WORKSPACE_JOB = "export_workspace"
DELETE_WORKSPACE_JOB = "delete_workspace"


def export_path(settings: Any, workspace_id: int, export_id: str) -> Any:
    """Where an export artifact lives: data dir, per workspace, never shared."""
    return settings.resolved_data_dir / "exports" / f"ws{workspace_id}" / f"{export_id}.json"


def run_export_workspace_job(database: Any, settings: Any, job: Any) -> None:
    """Write the workspace's export document to its artifact path."""
    workspace_id = int(job.payload["workspace_id"])
    export_id = str(job.payload["export_id"])
    with database.session() as session:
        document = export_workspace_data(session, workspace_id)
    path = export_path(settings, workspace_id, export_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".partial")
    temp.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    temp.replace(path)


def run_delete_workspace_job(database: Any, settings: Any, job: Any) -> None:
    """Detach every grant, then purge the workspace's rows and files."""
    from runway.jobs.queue import JobQueue
    from runway.platforms.registry import ProviderRegistry
    from runway.realtime import standby
    from runway.realtime.lifecycle import disconnect_account

    standby.ensure_active(settings)
    workspace_id = int(job.payload["workspace_id"])
    registry = ProviderRegistry(database, settings)
    queue = JobQueue(database)
    with database.session() as session:
        accounts = session.scalars(
            select(ConnectedAccount).where(
                ConnectedAccount.workspace_id == workspace_id,
                ConnectedAccount.status != "disconnected",
            )
        ).all()
        for account in accounts:
            disconnect_account(session, registry, queue, account)
        counts = purge_workspace(session, workspace_id)
        session.flush()
    for directory in (
        settings.resolved_data_dir / "exports" / f"ws{workspace_id}",
        settings.resolved_data_dir / "media" / f"ws{workspace_id}",
    ):
        shutil.rmtree(directory, ignore_errors=True)
    logger.info("workspace %s purged: %s", workspace_id, counts)
