"""Migration 0018b: `account_sync_state`, `quota_usage`, `workspaces.last_seen_at`.

Upgrading 0018 -> 0018b creates both tables with their indexes and foreign
keys and adds the workspace last-seen column; downgrading removes them and
restores the 0018 schema. A crash mid-revision rolls the whole revision
back, and a rerun after a partial upgrade completes what is missing instead
of tripping over what exists.
"""

from __future__ import annotations

import io
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine

from alembic import command
from runway.config import Settings
from runway.db.bootstrap import alembic_config, migration_head
from runway.db.schema_contract import load_schema_snapshot, schema_snapshot

HEAD = migration_head(Settings())
PREVIOUS = "0018_webhook_inbox"
SYNC_TABLES = {"account_sync_state", "quota_usage"}


def _config(settings: Settings):  # type: ignore[no-untyped-def]
    return alembic_config(settings)


def _engine(settings: Settings) -> Engine:
    return create_engine(settings.database_url)


def _snapshot(settings: Settings) -> dict[str, object]:
    engine = _engine(settings)
    try:
        return schema_snapshot(engine)
    finally:
        engine.dispose()


def _tables(settings: Settings) -> set[str]:
    engine = _engine(settings)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def _columns(settings: Settings, table: str) -> set[str]:
    engine = _engine(settings)
    try:
        return {column["name"] for column in inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def _version(settings: Settings) -> str | None:
    engine = _engine(settings)
    try:
        with engine.connect() as connection:
            row = connection.execute(text("SELECT version_num FROM alembic_version")).first()
        return None if row is None else str(row[0])
    finally:
        engine.dispose()


def _committed_head_fingerprint() -> str:
    snapshot = load_schema_snapshot(
        Settings().project_root / "docs" / "schema" / "intelligence-data-flywheel.json"
    )
    return str(snapshot["fingerprint"])


@pytest.fixture()
def at_0018(tmp_path: Path) -> Settings:
    """A database with the real 0018 schema (migration by migration)."""
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_directories()
    config = _config(settings)
    command.upgrade(config, "head")
    command.downgrade(config, PREVIOUS)
    return settings


def test_0018b_creates_and_removes_the_sync_state_tables(at_0018: Settings) -> None:
    config = _config(at_0018)
    before = _snapshot(at_0018)
    assert SYNC_TABLES.isdisjoint(_tables(at_0018))
    assert "last_seen_at" not in _columns(at_0018, "workspaces")

    command.upgrade(config, HEAD)

    assert _tables(at_0018) >= SYNC_TABLES
    assert "last_seen_at" in _columns(at_0018, "workspaces")
    inspector = inspect(_engine(at_0018))
    state_columns = {column["name"] for column in inspector.get_columns("account_sync_state")}
    assert {
        "id",
        "workspace_id",
        "account_id",
        "scope",
        "last_run_at",
        "last_status",
        "last_error",
        "next_run_at",
        "boosted_until",
        "source",
        "consecutive_failures",
        "updated_at",
    } <= state_columns
    state_uniques = {
        constraint["name"]: constraint["column_names"]
        for constraint in inspector.get_unique_constraints("account_sync_state")
    }
    assert state_uniques["uq_account_sync_state_scope"] == ["account_id", "scope"]
    state_foreign_keys = {
        (fk["referred_table"], tuple(fk["constrained_columns"])): fk["options"].get("ondelete")
        for fk in inspector.get_foreign_keys("account_sync_state")
    }
    assert state_foreign_keys[("connected_accounts", ("account_id",))] == "CASCADE"
    state_indexes = {
        index["name"]: index["column_names"]
        for index in inspector.get_indexes("account_sync_state")
    }
    assert "ix_account_sync_state_due" in state_indexes

    quota_columns = {column["name"] for column in inspector.get_columns("quota_usage")}
    assert {
        "id",
        "provider",
        "bucket",
        "day",
        "units_used",
        "updated_at",
    } <= quota_columns
    quota_uniques = {
        constraint["name"]: constraint["column_names"]
        for constraint in inspector.get_unique_constraints("quota_usage")
    }
    assert quota_uniques["uq_quota_usage_day"] == ["provider", "bucket", "day"]
    assert _version(at_0018) == HEAD

    command.downgrade(config, PREVIOUS)

    assert SYNC_TABLES.isdisjoint(_tables(at_0018))
    assert "last_seen_at" not in _columns(at_0018, "workspaces")
    assert _snapshot(at_0018)["fingerprint"] == before["fingerprint"]
    assert _version(at_0018) == PREVIOUS


def test_upgrade_from_0018_matches_the_committed_contract(at_0018: Settings) -> None:
    command.upgrade(_config(at_0018), HEAD)
    assert _snapshot(at_0018)["fingerprint"] == _committed_head_fingerprint()


# ------------------------------------------------------------- crash safety


class InjectedCrash(RuntimeError):
    pass


@contextmanager
def _crash_on(statement_prefix: str) -> Iterator[list[str]]:
    """Fail the first SQL statement starting with `statement_prefix`, in any engine."""
    seen: list[str] = []

    def hook(_conn, _cursor, statement, *_args) -> None:  # type: ignore[no-untyped-def]
        if statement.lstrip().upper().startswith(statement_prefix.upper()) and not seen:
            seen.append(statement)
            raise InjectedCrash(f"simulated crash at: {statement_prefix}")

    event.listen(Engine, "before_cursor_execute", hook)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", hook)


def test_a_crash_mid_0018b_rolls_the_revision_back_and_a_rerun_succeeds(
    at_0018: Settings,
) -> None:
    config = _config(at_0018)

    # Fails after account_sync_state was already created.
    with _crash_on("CREATE TABLE quota_usage") as hit, pytest.raises(InjectedCrash):
        command.upgrade(config, HEAD)
    assert hit, "the crash point was never reached"

    assert _version(at_0018) == PREVIOUS
    assert SYNC_TABLES.isdisjoint(_tables(at_0018))

    command.upgrade(config, HEAD)

    assert _version(at_0018) == HEAD
    assert _tables(at_0018) >= SYNC_TABLES
    assert _snapshot(at_0018)["fingerprint"] == _committed_head_fingerprint()


def test_0018b_completes_a_partial_run_that_created_only_account_sync_state(
    at_0018: Settings,
) -> None:
    config = _config(at_0018)
    command.upgrade(config, HEAD)
    engine = _engine(at_0018)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE quota_usage"))
        connection.execute(text("UPDATE alembic_version SET version_num = '0018_webhook_inbox'"))
    engine.dispose()

    command.upgrade(config, HEAD)

    assert _tables(at_0018) >= SYNC_TABLES
    assert _snapshot(at_0018)["fingerprint"] == _committed_head_fingerprint()


def test_0018b_readds_last_seen_at_when_it_is_missing(at_0018: Settings) -> None:
    """A partial run that created the tables but never added the column."""
    config = _config(at_0018)
    command.upgrade(config, HEAD)
    engine = _engine(at_0018)
    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE workspaces DROP COLUMN last_seen_at"))
        connection.execute(text("UPDATE alembic_version SET version_num = '0018_webhook_inbox'"))
    engine.dispose()

    command.upgrade(config, HEAD)

    assert "last_seen_at" in _columns(at_0018, "workspaces")
    assert _snapshot(at_0018)["fingerprint"] == _committed_head_fingerprint()


def test_postgres_upgrade_creates_both_tables_and_the_column(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "pg")
    config = _config(settings)
    config.set_main_option("sqlalchemy.url", "postgresql://runway@localhost/runway")
    buffer = io.StringIO()
    config.output_buffer = buffer
    command.upgrade(config, f"{PREVIOUS}:head", sql=True)
    sql = buffer.getvalue()

    assert "_alembic_tmp" not in sql
    assert "CREATE TABLE account_sync_state (" in sql
    assert "CREATE TABLE quota_usage (" in sql
    assert "ALTER TABLE workspaces ADD COLUMN last_seen_at" in sql
    assert "ON DELETE CASCADE" in sql
