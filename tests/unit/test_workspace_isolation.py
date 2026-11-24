"""Workspace tenancy: bound sessions filter reads and guard writes (phase P7).

These run on SQLite, where the application layer (loader criteria + the flush
guard) is the whole defense; `test_row_level_security_*` runs the Postgres
policy end to end when --postgres-dsn is passed (the backend-postgres CI job).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from sqlalchemy import select

from runway.db.base import Database
from runway.db.models import (
    Channel,
    ConnectedAccount,
    ContentDraft,
    ContentGroup,
    SyncRun,
    WebhookEndpoint,
    Workspace,
)
from runway.db.workspace import (
    SESSION_INFO_KEY,
    SYSTEM_INFO_KEY,
    WorkspaceContextError,
    WorkspaceIsolationError,
    scoped_models,
    workspace_context,
)

OTHER_WORKSPACE = 2


@pytest.fixture()
def two_workspaces(database: Database) -> Database:
    with database.system_session() as session:
        session.add(Workspace(id=OTHER_WORKSPACE, slug="other", name="Other"))
    return database


def _channel(workspace_id: int) -> Channel:
    return Channel(
        workspace_id=workspace_id,
        name=f"Channel {workspace_id}",
        handle=f"@channel-{workspace_id}",
        timezone="UTC",
        default_post_time="09:00",
        planning_horizon_days=7,
        duplicate_window_days=30,
    )


def _account(workspace_id: int) -> ConnectedAccount:
    return ConnectedAccount(
        workspace_id=workspace_id,
        platform="tiktok",
        external_account_id=f"ext-{workspace_id}",
        status="connected",
        granted_scopes_json="[]",
    )


def _draft(workspace_id: int) -> ContentDraft:
    return ContentDraft(
        workspace_id=workspace_id, title=f"Draft {workspace_id}", body="", status="draft"
    )


def _group(workspace_id: int) -> ContentGroup:
    return ContentGroup(workspace_id=workspace_id, title=f"Group {workspace_id}")


def _endpoint(workspace_id: int) -> WebhookEndpoint:
    return WebhookEndpoint(
        workspace_id=workspace_id, url="https://example.test/hook", secret_encrypted="enc"
    )


# One row factory per representative scoped model (every scoped model shares
# the same criteria path, so covering each shape of table is enough).
ROW_FACTORIES: list[tuple[type, Callable[[int], object]]] = [
    (Channel, _channel),
    (ConnectedAccount, _account),
    (ContentDraft, _draft),
    (ContentGroup, _group),
    (WebhookEndpoint, _endpoint),
]

_MODEL_IDS = [model.__name__ for model, _ in ROW_FACTORIES]


@pytest.mark.parametrize(("model", "factory"), ROW_FACTORIES, ids=_MODEL_IDS)
def test_bound_session_reads_only_its_workspace(
    two_workspaces: Database, model: type, factory: Callable[[int], object]
) -> None:
    with two_workspaces.system_session() as session:
        session.add(factory(1))
        session.add(factory(OTHER_WORKSPACE))
    with two_workspaces.session(workspace_id=1) as session:
        rows = session.scalars(select(model)).all()
        assert rows and all(row.workspace_id == 1 for row in rows)
    with two_workspaces.session(workspace_id=OTHER_WORKSPACE) as session:
        rows = session.scalars(select(model)).all()
        assert all(row.workspace_id == OTHER_WORKSPACE for row in rows)


@pytest.mark.parametrize(("model", "factory"), ROW_FACTORIES, ids=_MODEL_IDS)
def test_session_get_cannot_cross_workspaces(
    two_workspaces: Database, model: type, factory: Callable[[int], object]
) -> None:
    with two_workspaces.system_session() as session:
        foreign = factory(OTHER_WORKSPACE)
        session.add(foreign)
        session.flush()
        foreign_id = foreign.id  # type: ignore[attr-defined]
    with two_workspaces.session(workspace_id=1) as session:
        assert session.get(model, foreign_id) is None
    with two_workspaces.session(workspace_id=OTHER_WORKSPACE) as session:
        assert session.get(model, foreign_id) is not None


@pytest.mark.parametrize(("model", "factory"), ROW_FACTORIES, ids=_MODEL_IDS)
def test_system_session_sees_every_workspace(
    two_workspaces: Database, model: type, factory: Callable[[int], object]
) -> None:
    with two_workspaces.system_session() as session:
        session.add_all([factory(1), factory(OTHER_WORKSPACE)])
    with two_workspaces.system_session() as session:
        workspaces = {row.workspace_id for row in session.scalars(select(model)).all()}
        assert workspaces == {1, OTHER_WORKSPACE}


def test_cross_workspace_insert_is_rejected(two_workspaces: Database) -> None:
    # pytest.raises wraps the session context: the guard fires at flush, and
    # the context manager's rollback then unwinds cleanly.
    with (
        pytest.raises(WorkspaceIsolationError),
        two_workspaces.session(workspace_id=1) as session,
    ):
        session.add(_account(OTHER_WORKSPACE))
        session.flush()


def test_unset_workspace_insert_is_rejected_outside_default(two_workspaces: Database) -> None:
    # The column default would silently file the row under workspace 1.
    with (
        pytest.raises(WorkspaceIsolationError),
        two_workspaces.session(workspace_id=OTHER_WORKSPACE) as session,
    ):
        session.add(
            ConnectedAccount(
                platform="tiktok",
                external_account_id="unbound",
                status="connected",
                granted_scopes_json="[]",
            )
        )
        session.flush()


def test_smuggled_foreign_row_is_rejected_on_flush(two_workspaces: Database) -> None:
    with two_workspaces.system_session() as session:
        account = _account(OTHER_WORKSPACE)
        session.add(account)
        session.flush()
        account_id = account.id
    # merge() selects first (filtered out by the criteria), then stages an
    # insert the flush guard refuses.
    with (
        pytest.raises(WorkspaceIsolationError),
        two_workspaces.session(workspace_id=1) as session,
    ):
        session.merge(
            ConnectedAccount(
                id=account_id,
                workspace_id=OTHER_WORKSPACE,
                platform="tiktok",
                external_account_id="smuggled",
                status="connected",
                granted_scopes_json="[]",
            )
        )
        session.flush()


def test_system_session_is_marked_not_bound(database: Database) -> None:
    """The GUC listener reads the system flag; a bound id must not be there."""
    with database.system_session() as session:
        assert session.info[SYSTEM_INFO_KEY] is True
        assert SESSION_INFO_KEY not in session.info


def test_postgres_sessions_emit_their_gucs(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """after_begin emits app.system for system sessions, else the bound id.

    RLS fails closed: a session that emits neither GUC sees zero rows, so
    system sessions have to be marked - not just left unbound.
    """
    from sqlalchemy.engine import Connection

    emitted: list[str] = []
    monkeypatch.setattr(database, "dialect", "postgresql")
    monkeypatch.setattr(
        Connection,
        "exec_driver_sql",
        lambda _connection, statement, *args, **kwargs: emitted.append(statement),
    )
    with database.session(workspace_id=OTHER_WORKSPACE) as session:
        session.connection()  # autobegin fires after_begin
    assert emitted == ["SET LOCAL app.workspace_id = 2"]
    emitted.clear()
    with database.system_session() as session:
        session.connection()
    assert emitted == ["SET LOCAL app.system = 'on'"]
    emitted.clear()
    # No marks, no GUC: a raw session would see zero rows under fail-closed RLS.
    raw = database.session_factory()
    try:
        raw.connection()
        assert emitted == []
    finally:
        raw.close()


def test_queue_and_engine_exclusive_sessions_run_as_system(database: Database) -> None:
    """Claim and engine locks cross workspaces: they must be system sessions."""
    from runway.jobs.queue import JobQueue
    from runway.platforms.publisher_registry import PublisherRegistry
    from runway.posts.engine import PublishingEngine

    with JobQueue(database)._exclusive() as session:
        assert session.info[SYSTEM_INFO_KEY] is True
        assert SESSION_INFO_KEY not in session.info
    engine = PublishingEngine(database, database.settings, PublisherRegistry(mock_mode=True))
    with engine._exclusive() as session:
        assert session.info[SYSTEM_INFO_KEY] is True
        assert SESSION_INFO_KEY not in session.info


def test_workspace_context_binds_sessions(two_workspaces: Database) -> None:
    with two_workspaces.system_session() as session:
        session.add_all([_account(1), _account(OTHER_WORKSPACE)])
    with workspace_context(OTHER_WORKSPACE), two_workspaces.session() as session:
        assert {row.workspace_id for row in session.scalars(select(ConnectedAccount)).all()} == {
            OTHER_WORKSPACE
        }


def test_sync_run_isolation_uses_foreign_account(two_workspaces: Database) -> None:
    with two_workspaces.system_session() as session:
        foreign_account = _account(OTHER_WORKSPACE)
        session.add(foreign_account)
        session.flush()
        run = SyncRun(
            account_id=foreign_account.id,
            trigger="manual",
            status="failed",
            workspace_id=OTHER_WORKSPACE,
        )
        session.add(run)
        session.flush()
        run_id = run.id
    with two_workspaces.session(workspace_id=1) as session:
        assert session.scalars(select(SyncRun)).all() == []
    with two_workspaces.session(workspace_id=OTHER_WORKSPACE) as session:
        assert [row.id for row in session.scalars(select(SyncRun)).all()] == [run_id]


def test_every_scoped_table_is_covered_by_the_rls_migration() -> None:
    import importlib.util
    from pathlib import Path

    migration = (
        Path(__file__).resolve().parents[2] / "alembic" / "versions" / "0021_row_level_security.py"
    )
    spec = importlib.util.spec_from_file_location("rls_migration", migration)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    rls_tables = set(module.SCOPED_TABLES)
    scoped_tables = {model.__tablename__ for model in scoped_models()}
    assert scoped_tables <= rls_tables, (
        f"scoped tables missing from 0021_row_level_security: {scoped_tables - rls_tables}"
    )


def test_hosted_session_without_context_refuses(
    two_workspaces: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(two_workspaces.settings, "profile", "hosted")
    with pytest.raises(WorkspaceContextError), two_workspaces.session():
        pass


@pytest.mark.postgres()
def test_row_level_security_enforces_isolation(request: pytest.FixtureRequest) -> None:
    """On Postgres the policy below the ORM must enforce the same boundary."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import SQLAlchemyError

    dsn = request.config.getoption("--postgres-dsn")
    if not dsn:
        pytest.skip("pass --postgres-dsn=postgresql+psycopg://... to run the RLS checks")
    engine = create_engine(dsn)
    with engine.connect() as connection:
        # App role semantics: a bound transaction sees only its workspace.
        connection.exec_driver_sql("SET LOCAL app.workspace_id = '1'")
        rows = connection.exec_driver_sql(
            "SELECT DISTINCT workspace_id FROM connected_accounts"
        ).all()
        assert all(row[0] == 1 for row in rows)
        # Cross-workspace write blocked by WITH CHECK.
        with pytest.raises(SQLAlchemyError):
            connection.exec_driver_sql(
                "INSERT INTO content_groups (title, workspace_id) VALUES ('x', 2)"
            )
        connection.rollback()
    engine.dispose()


@pytest.mark.postgres()
def test_row_level_security_fails_closed_without_gucs(request: pytest.FixtureRequest) -> None:
    """A session with neither GUC sees no rows; app.system = 'on' sees them all."""
    from sqlalchemy import create_engine
    from sqlalchemy.exc import SQLAlchemyError

    dsn = request.config.getoption("--postgres-dsn")
    if not dsn:
        pytest.skip("pass --postgres-dsn=postgresql+psycopg://... to run the RLS checks")
    engine = create_engine(dsn)
    try:
        with engine.connect() as connection:
            # Seed through the system GUC, which is what system_session emits.
            connection.exec_driver_sql("SET LOCAL app.system = 'on'")
            connection.exec_driver_sql(
                "INSERT INTO content_groups (title, workspace_id) VALUES ('rls-seed', 1)"
            )
            connection.commit()
        try:
            with engine.connect() as connection:
                # Neither app.system nor app.workspace_id set: fail closed.
                assert (
                    connection.exec_driver_sql("SELECT count(*) FROM content_groups").scalar() == 0
                )
                with pytest.raises(SQLAlchemyError):
                    connection.exec_driver_sql(
                        "INSERT INTO content_groups (title, workspace_id) VALUES ('x', 1)"
                    )
                connection.rollback()
            with engine.connect() as connection:
                connection.exec_driver_sql("SET LOCAL app.system = 'on'")
                assert (
                    connection.exec_driver_sql(
                        "SELECT count(*) FROM content_groups WHERE title = 'rls-seed'"
                    ).scalar()
                    == 1
                )
        finally:
            with engine.connect() as connection:
                connection.exec_driver_sql("SET LOCAL app.system = 'on'")
                connection.exec_driver_sql("DELETE FROM content_groups WHERE title = 'rls-seed'")
                connection.commit()
    finally:
        engine.dispose()
