"""Workspace isolation primitives (phase P7).

Three cooperating layers keep one workspace from ever seeing another's rows:

- :class:`WorkspaceScoped` (defined on the models in ``runway.db.models``)
  marks an ORM model whose rows carry a ``workspace_id`` tenant column.
  Every ``session()`` runs under a workspace context: a ``do_orm_execute``
  listener appends ``workspace_id = <context>`` to every SELECT through
  ``with_loader_criteria``, and a ``before_flush`` guard refuses inserts or
  moves aimed at a different workspace.
- The workspace context is a :class:`contextvars.ContextVar`. Request handlers
  get it from a FastAPI dependency that copies ``principal.workspace_id``;
  worker jobs set it per job in the worker loop. In the ``local`` profile a
  bare ``database.session()`` defaults to workspace 1; in ``hosted`` it
  raises :class:`WorkspaceContextError`, so context-free code must say so
  explicitly via :meth:`Database.system_session`.
- On Postgres the same context is mirrored to the database with
  ``SET LOCAL app.workspace_id`` so row-level security applies the same rule
  server-side (migration ``0021_row_level_security``).

``system_session()`` is the escape hatch for system code whose job is
cross-workspace by definition: the publish queue claim bucket, webhook ingest
before the account (and therefore workspace) is known, api-key resolution,
retention sweeps and operator CLI commands. It applies no criteria and no
guard and is only ever opened by the call sites that need it. On Postgres it
emits ``SET LOCAL app.system = 'on'``: the RLS policies fail closed, so a
session with neither GUC sees zero rows and only an explicit system session
(or a ``BYPASSRLS`` role) sees every row.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from runway.db.base import Base

# The GUC names the RLS policies read; mirrored into session.info on ORM
# sessions and emitted as `SET LOCAL` inside Postgres transactions.
GUC_NAME = "app.workspace_id"
SESSION_INFO_KEY = "workspace_id"
# `app.system = 'on'` marks a system session for RLS. The policies grant rows
# only to this flag or an `app.workspace_id` match, so a session with neither
# fails closed instead of silently seeing every workspace.
SYSTEM_GUC_NAME = "app.system"
SYSTEM_INFO_KEY = "system"


class WorkspaceContextError(RuntimeError):
    """`session()` was asked for a workspace-bound session without a context.

    In the `hosted` profile a request handler always runs inside a principal
    context and a worker handler inside the job's. Any other caller - queue
    internals, producers, sweeps, CLI - is system code and must open a
    `system_session()` (or pass `workspace_id=` explicitly).
    """


class WorkspaceIsolationError(RuntimeError):
    """A write in a workspace-bound session targeted a different workspace."""


_workspace_context: ContextVar[int | None] = ContextVar("runway_workspace", default=None)


def current_workspace() -> int | None:
    """The workspace context of the current task, if one was set."""
    return _workspace_context.get()


@contextmanager
def workspace_context(workspace_id: int) -> Iterator[None]:
    """Set the workspace context for the enclosed block (worker jobs, tests)."""
    token = _workspace_context.set(workspace_id)
    try:
        yield
    finally:
        _workspace_context.reset(token)


def scoped_models() -> list[type[Base]]:
    """Every mapped class marked `WorkspaceScoped`, in table-name order."""
    from runway.db.base import Base
    from runway.db.models import WorkspaceScoped

    # Every WorkspaceScoped model also inherits Base, which the issubclass
    # narrowing does not tell mypy; cast to the ORM base explicitly.
    models = cast(
        list[type[Base]],
        [
            mapper.class_
            for mapper in Base.registry.mappers
            if isinstance(mapper.class_, type) and issubclass(mapper.class_, WorkspaceScoped)
        ],
    )
    return sorted(models, key=lambda model: model.__tablename__)


def scoped_table_names() -> frozenset[str]:
    """Table names of every `WorkspaceScoped` model (the RLS table list)."""
    return frozenset(model.__tablename__ for model in scoped_models())


# workspace_id-bearing tables that are intentionally NOT tenant-scoped for
# app-layer filtering and RLS. Each entry is system or directory data whose
# correct operation is cross-workspace by definition; the metadata invariant
# test allows exactly this list and nothing more.
UNSCOPED_WORKSPACE_TABLES = frozenset(
    {
        # The publish-job claim bucket: workers claim whichever job is due,
        # then scope themselves to that job's workspace. Raw SQL, not ORM.
        "publish_jobs",
        # Raw platform webhook bodies, stored before verification resolves
        # the account; workspace_id stays NULL until process_webhook stamps it.
        "webhook_events",
        # org -> workspace directory: consulted during hosted sign-in before
        # the caller's workspace is known.
        "workspace_identity_links",
        # svix delivery dedupe for identity webhooks; not tenant data.
        "identity_webhook_deliveries",
    }
)
