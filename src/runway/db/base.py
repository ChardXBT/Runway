from __future__ import annotations

import itertools
from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker, with_loader_criteria

from runway.config import Settings
from runway.db.workspace import (
    GUC_NAME,
    SESSION_INFO_KEY,
    SYSTEM_GUC_NAME,
    SYSTEM_INFO_KEY,
    WorkspaceContextError,
    WorkspaceIsolationError,
    current_workspace,
)


class Base(DeclarativeBase):
    pass


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def set_pragmas(dbapi_connection: object, _connection_record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


def _workspace_criteria(execute_state: object) -> None:
    """Append `workspace_id = <session workspace>` to every ORM SELECT.

    Only sessions created through `Database.session()`/`session(workspace_id=)`
    carry `session.info["workspace_id"]`; `system_session()` deliberately does
    not, so its queries stay unfiltered. Bulk `update()`/`delete()` statements
    are not SELECTs: code using them keeps writing its own workspace filter,
    and on Postgres the RLS policy applies the same bound server-side.
    """
    session: Session = execute_state.session  # type: ignore[attr-defined]
    workspace_id = session.info.get(SESSION_INFO_KEY)
    if workspace_id is None:
        return
    if not execute_state.is_select:  # type: ignore[attr-defined]
        return
    if (
        execute_state.is_column_load  # type: ignore[attr-defined]
        or execute_state.is_relationship_load  # type: ignore[attr-defined]
    ):
        return
    if execute_state.execution_options.get("unfiltered_workspace"):  # type: ignore[attr-defined]
        return
    from runway.db.workspace import scoped_models

    options = tuple(
        with_loader_criteria(
            model,
            model.workspace_id == workspace_id,  # type: ignore[attr-defined]
            include_aliases=True,
        )
        for model in scoped_models()
    )
    execute_state.statement = execute_state.statement.options(*options)  # type: ignore[attr-defined]


def _guard_workspace_writes(session: Session, _flush_context: object, _instances: object) -> None:
    """Refuse inserts or moves that would leave the session's workspace.

    A `WorkspaceScoped` row whose `workspace_id` differs from the session
    binding (or is unset while the binding is not the default workspace, where
    the column default would silently file it under workspace 1) is a bug or
    an attack; either way it raises before touching the database.
    """
    workspace_id = session.info.get(SESSION_INFO_KEY)
    if workspace_id is None:
        return
    from runway.db.models import DEFAULT_WORKSPACE_ID, WorkspaceScoped

    for obj in itertools.chain(session.new, session.dirty, session.deleted):
        if not isinstance(obj, WorkspaceScoped):
            continue
        row_workspace = obj.workspace_id
        if row_workspace is None:
            if obj in session.new and workspace_id != DEFAULT_WORKSPACE_ID:
                raise WorkspaceIsolationError(
                    f"{type(obj).__name__} has no workspace_id set; in workspace "
                    f"{workspace_id} it must be set explicitly (the column default "
                    f"would write workspace {DEFAULT_WORKSPACE_ID})"
                )
            continue
        if row_workspace != workspace_id:
            raise WorkspaceIsolationError(
                f"{type(obj).__name__} belongs to workspace {row_workspace}; "
                f"this session is bound to workspace {workspace_id}"
            )


def _set_workspace_guc(
    database: Database, session: Session, connection: object, tx: object
) -> None:
    """Mirror the session's tenancy into Postgres for row-level security.

    RLS fails closed - a session needs one of the two GUCs to see anything -
    so the system flag is emitted explicitly rather than left unset.
    """
    if database.dialect != "postgresql":
        return
    if tx.nested:  # type: ignore[attr-defined]
        return
    # connection is the driver's ConnectionFairy, not the ORM Session
    if session.info.get(SYSTEM_INFO_KEY):
        connection.exec_driver_sql(f"SET LOCAL {SYSTEM_GUC_NAME} = 'on'")  # type: ignore[attr-defined]
        return
    workspace_id = session.info.get(SESSION_INFO_KEY)
    if workspace_id is None:
        return
    connection.exec_driver_sql(f"SET LOCAL {GUC_NAME} = {int(workspace_id)}")  # type: ignore[attr-defined]


class Database:
    def __init__(self, settings: Settings):
        settings.ensure_directories()
        self.settings = settings
        url = settings.database_url
        is_sqlite = url.startswith("sqlite")
        connect_args = {"check_same_thread": False} if is_sqlite else {}
        self.engine = create_engine(url, connect_args=connect_args, future=True)
        self.dialect: str = self.engine.dialect.name
        if is_sqlite:
            _configure_sqlite(self.engine)
        system_url = settings.system_database_url
        # An optional dedicated role for system sessions; when unset (or equal
        # to the app URL) system sessions share the app engine and rely on the
        # app.system GUC the after_begin listener below emits.
        self.system_engine: Engine | None = (
            create_engine(system_url, future=True)
            if system_url is not None and system_url != url
            else None
        )
        self.session_factory = sessionmaker(
            bind=self.engine,
            class_=Session,
            expire_on_commit=False,
            autoflush=False,
        )
        self.system_session_factory = (
            sessionmaker(
                bind=self.system_engine,
                class_=Session,
                expire_on_commit=False,
                autoflush=False,
            )
            if self.system_engine is not None
            else self.session_factory
        )
        event.listen(self.session_factory, "do_orm_execute", _workspace_criteria)
        event.listen(self.session_factory, "before_flush", _guard_workspace_writes)

        def emit_guc(session: Session, tx: object, connection: object) -> None:
            _set_workspace_guc(self, session, connection, tx)

        event.listen(self.session_factory, "after_begin", emit_guc)
        if self.system_session_factory is not self.session_factory:
            # A dedicated system engine still needs the GUC listener: without
            # it a non-bypass system role would see zero rows under fail-closed
            # RLS. When the factories are one object the listener is already on.
            event.listen(self.system_session_factory, "after_begin", emit_guc)

    def _workspace_for(self, workspace_id: int | None) -> int:
        """Resolve a workspace binding: explicit arg, then context, then profile."""
        if workspace_id is not None:
            return workspace_id
        workspace_id = current_workspace()
        if workspace_id is not None:
            return workspace_id
        if self.settings.profile == "hosted":
            raise WorkspaceContextError(
                "session() with no workspace context is system code in the hosted "
                "profile; use system_session() or pass workspace_id="
            )
        from runway.db.models import DEFAULT_WORKSPACE_ID

        return DEFAULT_WORKSPACE_ID

    @contextmanager
    def session(self, workspace_id: int | None = None) -> Generator[Session, None, None]:
        """A workspace-bound session: criteria-filtered reads, guarded writes.

        The binding comes from `workspace_id`, else the current workspace
        context (request principal or worker job), else - `local` profile
        only - the default workspace. On Postgres it also emits
        `SET LOCAL app.workspace_id` per transaction for row-level security.
        """
        bound = self._workspace_for(workspace_id)
        session = self.session_factory(info={SESSION_INFO_KEY: bound})
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    @contextmanager
    def system_session(self) -> Generator[Session, None, None]:
        """A context-free session for system code only (queue, sweeps, ingest).

        No workspace criteria and no write guard; `session.info["system"]`
        marks it. On Postgres it emits `SET LOCAL app.system = 'on'`: the RLS
        policies fail closed for sessions with neither GUC, so only an
        explicit system session (or a BYPASSRLS role) sees every row.
        RUNWAY_SYSTEM_DATABASE_URL can still point system traffic at a
        dedicated role as defense in depth.
        """
        session = self.system_session_factory(info={SYSTEM_INFO_KEY: True})
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
