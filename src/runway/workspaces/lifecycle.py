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

from sqlalchemy import delete, func, inspect, select, tuple_
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
    """Mapped tables carrying a `workspace_id` column (the export's row set)."""
    return [table for table in Base.metadata.tables.values() if "workspace_id" in table.c]


# ------------------------------------------------------------- purge plan
#
# `workspace_id` only exists on the top-level tables; everything the
# pipeline hangs below them (channels' proposals, slates, candidates, ...)
# carries no workspace key of its own and is reached by following foreign
# keys to a fixpoint. SQLite enforces FKs immediately, so the delete plan
# orders children before parents, and the schema's one FK cycle -
# `proposals.caption_slate_id` <-> `caption_slates.proposal_id`, both
# nullable - is broken by clearing those links first.

_BIND_CHUNK = 500  # well under SQLite's bound-parameter limit


def _chunks(values: Any) -> Any:
    items = list(values)
    for start in range(0, len(items), _BIND_CHUNK):
        yield items[start : start + _BIND_CHUNK]


def _pk_filter(table: Any, ids: list[tuple[Any, ...]]) -> Any:
    pk = list(table.primary_key.columns)
    if len(pk) == 1:
        return pk[0].in_([row[0] for row in ids])
    return tuple_(*pk).in_(ids)


def _owned_rows(session: Session, workspace_id: int) -> dict[str, set[tuple[Any, ...]]]:
    """Primary keys of every row that belongs to the workspace.

    Seeded from the tables carrying `workspace_id`, then expanded through
    foreign keys until stable: a row that references an owned row is owned
    as well - its parent could not be deleted while it survives anyway.
    Only rounds where a parent set grew re-scan its children.
    """
    tables = Base.metadata.tables
    owned: dict[str, set[tuple[Any, ...]]] = {name: set() for name in tables}
    for table in tables.values():
        if "workspace_id" not in table.c:
            continue
        rows = session.execute(
            select(*table.primary_key.columns).where(table.c.workspace_id == workspace_id)
        ).all()
        owned[table.name] = {tuple(row) for row in rows}
    pending = {name for name, ids in owned.items() if ids}
    while pending:
        grown: set[str] = set()
        for table in tables.values():
            found: set[tuple[Any, ...]] = set()
            for fk in table.foreign_keys:
                parent = fk.column.table
                if parent.name not in pending or fk.column not in list(parent.primary_key.columns):
                    continue
                keys = {key[0] for key in owned[parent.name]}
                for chunk in _chunks(keys):
                    rows = session.execute(
                        select(*table.primary_key.columns).where(fk.parent.in_(chunk))
                    ).all()
                    found |= {tuple(row) for row in rows}
            if found - owned[table.name]:
                owned[table.name] |= found
                grown.add(table.name)
        pending = grown
    return {name: ids for name, ids in owned.items() if ids}


def _dependency_edges(names: set[str], nulled: set[tuple[str, str]]) -> dict[str, set[str]]:
    """child -> parents that must outlive it inside the owned set.

    Self references need no ordering (one statement removes the set), and a
    link `purge_workspace` cleared no longer constrains the order.
    """
    tables = Base.metadata.tables
    edges: dict[str, set[str]] = {}
    for name in names:
        parents = {
            fk.column.table.name
            for fk in tables[name].foreign_keys
            if fk.column.table.name != name
            and fk.column.table.name in names
            and (name, fk.parent.name) not in nulled
        }
        if parents:
            edges[name] = parents
    return edges


def _cycles(edges: dict[str, set[str]]) -> list[set[str]]:
    """Strongly connected components of size > 1 - FK cycles no order resolves."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    stacked: set[str] = set()
    stack: list[str] = []
    counter = 0
    found: list[set[str]] = []

    def visit(node: str) -> None:
        nonlocal counter
        index[node] = low[node] = counter
        counter += 1
        stack.append(node)
        stacked.add(node)
        for parent in edges.get(node, ()):
            if parent not in index:
                visit(parent)
                low[node] = min(low[node], low[parent])
            elif parent in stacked:
                low[node] = min(low[node], index[parent])
        if low[node] == index[node]:
            component: set[str] = set()
            while True:
                member = stack.pop()
                stacked.discard(member)
                component.add(member)
                if member == node:
                    break
            if len(component) > 1:
                found.append(component)

    for node in edges:
        if node not in index:
            visit(node)
    return found


def _break_cycles(session: Session, owned: dict[str, set[tuple[Any, ...]]]) -> set[tuple[str, str]]:
    """Clear nullable owned->owned links inside FK cycles; return cleared (table, column)."""
    tables = Base.metadata.tables
    nulled: set[tuple[str, str]] = set()
    while True:
        cycles = _cycles(_dependency_edges(set(owned), nulled))
        if not cycles:
            return nulled
        progress = False
        for cycle in cycles:
            for name in cycle:
                table = tables[name]
                for fk in table.foreign_keys:
                    if (
                        fk.column.table.name in cycle - {name}
                        and fk.parent.nullable
                        and (name, fk.parent.name) not in nulled
                    ):
                        column = table.c[fk.parent.name]
                        for chunk in _chunks(owned[name]):
                            session.execute(
                                table.update()
                                .where(_pk_filter(table, chunk))
                                .where(column.is_not(None))
                                .values({fk.parent.name: None})
                            )
                        nulled.add((name, fk.parent.name))
                        progress = True
        if not progress:
            raise Problem.of(
                ProblemCode.INVALID_STATE,
                "workspace data has a foreign-key cycle no nullable link can break",
            )


def _delete_order(names: set[str], nulled: set[tuple[str, str]]) -> list[str]:
    """Owned tables, children before parents (Kahn on child -> parent edges)."""
    edges = _dependency_edges(names, nulled)
    waiting: dict[str, int] = {name: 0 for name in names}
    for parents in edges.values():
        for parent in parents:
            waiting[parent] += 1
    ready = sorted(name for name, count in waiting.items() if count == 0)
    order: list[str] = []
    while ready:
        name = ready.pop()
        order.append(name)
        for parent in edges.get(name, ()):
            waiting[parent] -= 1
            if waiting[parent] == 0:
                ready.append(parent)
    if len(order) != len(names):
        raise Problem.of(
            ProblemCode.INVALID_STATE,
            "workspace data has a foreign-key cycle no nullable link can break",
        )
    return order


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
    """Delete every row the workspace owns, then the workspace. Returns counts.

    Rows without a `workspace_id` of their own - the whole channel-scoped
    subgraph - are reached by following foreign keys, children are deleted
    before parents, and the proposals/caption_slates cycle is broken by
    clearing its nullable links first (see the helpers above).
    """
    owned = _owned_rows(session, workspace_id)
    nulled = _break_cycles(session, owned)
    counts: dict[str, int] = {}
    for name in _delete_order(set(owned), nulled):
        table = Base.metadata.tables[name]
        ids = sorted(owned[name])
        for chunk in _chunks(ids):
            session.execute(delete(table).where(_pk_filter(table, chunk)))
        counts[name] = len(ids)
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
