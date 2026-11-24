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
    """Mirror the session workspace into Postgres for row-level security."""
    if database.dialect != "postgresql":
        return
    if tx.nested:  # type: ignore[attr-defined]
        return
    workspace_id = session.info.get(SESSION_INFO_KEY)
    if workspace_id is None:
        return
    # connection is the driver's ConnectionFairy, not the ORM Session
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
        # A distinct bypass-RLS role for system sessions; when unset (or equal
        # to the app URL) system sessions share the app engine and fail closed
        # under row-level security.
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
        event.listen(
            self.session_factory,
            "after_begin",
            lambda session, tx, connection: _set_workspace_guc(self, session, connection, tx),
        )

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

        No workspace criteria, no write guard and no GUC. On Postgres the RLS
        policy's unset-setting branch (`nullif(current_setting(...), '') IS
        NULL`) grants visibility to any session that never set the GUC, so the
        app role suffices; RUNWAY_SYSTEM_DATABASE_URL adds a dedicated role
        for system traffic as defense in depth.
        """
        session = self.system_session_factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
