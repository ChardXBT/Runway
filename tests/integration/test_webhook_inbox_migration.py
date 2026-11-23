"""Migration 0018: the webhook inbox tables (`webhook_events`, `platform_subscriptions`).

Upgrading 0016 -> 0018 creates both tables with their indexes and foreign keys;
downgrading removes them and restores the 0016 schema. A crash mid-revision
rolls the whole revision back, and a rerun after a partial upgrade completes
what is missing instead of tripping over what exists.
"""

from __future__ import annotations

import io
import sqlite3
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
PREVIOUS = "0016_shared_contracts"
WEBHOOK_TABLES = {"webhook_events", "platform_subscriptions"}


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
def at_0016(tmp_path: Path) -> Settings:
    """A database with the real 0016 schema.

    Upgrading to head and downgrading to 0016 reproduces a database built
    migration by migration: revision 0001 creates the current models, so a
    fresh `upgrade 0016` would already contain the 0018 tables.
    """
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_directories()
    config = _config(settings)
    command.upgrade(config, "head")
    command.downgrade(config, PREVIOUS)
    return settings


def test_0018_creates_and_removes_the_webhook_inbox_tables(at_0016: Settings) -> None:
    config = _config(at_0016)
    before = _snapshot(at_0016)
    assert WEBHOOK_TABLES.isdisjoint(_tables(at_0016))

    command.upgrade(config, HEAD)

    assert _tables(at_0016) >= WEBHOOK_TABLES
    inspector = inspect(_engine(at_0016))
    events_indexes = {
        index["name"]: index["column_names"] for index in inspector.get_indexes("webhook_events")
    }
    assert events_indexes["ix_webhook_events_provider_received"] == [
        "provider",
        "received_at",
    ]
    assert "ix_webhook_events_processed_at" in events_indexes
    event_columns = {column["name"] for column in inspector.get_columns("webhook_events")}
    assert {
        "id",
        "provider",
        "subscription_id",
        "received_at",
        "signature_ok",
        "dedupe_key",
        "body",
        "content_type",
        "source",
        "processed_at",
        "error",
        "account_id",
        "workspace_id",
    } <= event_columns
    events_foreign_keys = {
        (fk["referred_table"], tuple(fk["constrained_columns"])): fk["options"].get("ondelete")
        for fk in inspector.get_foreign_keys("webhook_events")
    }
    assert events_foreign_keys[("platform_subscriptions", ("subscription_id",))] == "SET NULL"
    assert events_foreign_keys[("connected_accounts", ("account_id",))] == "SET NULL"
    event_uniques = {
        constraint["name"]: constraint["column_names"]
        for constraint in inspector.get_unique_constraints("webhook_events")
    }
    assert event_uniques["uq_webhook_events_dedupe_key"] == ["dedupe_key"]

    subscription_columns = {
        column["name"] for column in inspector.get_columns("platform_subscriptions")
    }
    assert {
        "id",
        "workspace_id",
        "account_id",
        "provider",
        "topic",
        "hub_secret",
        "lease_seconds",
        "subscribed_at",
        "expires_at",
        "state",
        "last_error",
        "created_at",
        "updated_at",
    } <= subscription_columns
    subscription_uniques = {
        constraint["name"]: constraint["column_names"]
        for constraint in inspector.get_unique_constraints("platform_subscriptions")
    }
    assert subscription_uniques["uq_platform_subscription"] == [
        "account_id",
        "provider",
        "topic",
    ]
    subscription_foreign_keys = {
        (fk["referred_table"], tuple(fk["constrained_columns"])): fk["options"].get("ondelete")
        for fk in inspector.get_foreign_keys("platform_subscriptions")
    }
    assert subscription_foreign_keys[("connected_accounts", ("account_id",))] == "CASCADE"
    assert subscription_foreign_keys[("workspaces", ("workspace_id",))] is None
    assert _version(at_0016) == HEAD

    command.downgrade(config, PREVIOUS)

    assert WEBHOOK_TABLES.isdisjoint(_tables(at_0016))
    assert _snapshot(at_0016)["fingerprint"] == before["fingerprint"]
    assert _version(at_0016) == PREVIOUS


def test_upgrade_from_0016_matches_the_committed_contract(at_0016: Settings) -> None:
    command.upgrade(_config(at_0016), HEAD)
    assert _snapshot(at_0016)["fingerprint"] == _committed_head_fingerprint()


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


def test_a_crash_mid_0018_rolls_the_revision_back_and_a_rerun_succeeds(
    at_0016: Settings,
) -> None:
    config = _config(at_0016)

    # Fails after platform_subscriptions was already created.
    with _crash_on("CREATE TABLE webhook_events") as hit, pytest.raises(InjectedCrash):
        command.upgrade(config, HEAD)
    assert hit, "the crash point was never reached"

    # The whole revision rolled back: still at 0016, no table left behind.
    assert _version(at_0016) == PREVIOUS
    assert WEBHOOK_TABLES.isdisjoint(_tables(at_0016))

    command.upgrade(config, HEAD)

    assert _version(at_0016) == HEAD
    assert _tables(at_0016) >= WEBHOOK_TABLES
    assert _snapshot(at_0016)["fingerprint"] == _committed_head_fingerprint()


def test_0018_completes_a_partial_run_that_created_only_platform_subscriptions(
    at_0016: Settings,
) -> None:
    """The state a pre-fix crash left: version 0016 but platform_subscriptions exists."""
    config = _config(at_0016)
    command.upgrade(config, HEAD)
    engine = _engine(at_0016)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE webhook_events"))
        connection.execute(text("UPDATE alembic_version SET version_num = '0016_shared_contracts'"))
    engine.dispose()

    command.upgrade(config, HEAD)

    assert _tables(at_0016) >= WEBHOOK_TABLES
    assert _snapshot(at_0016)["fingerprint"] == _committed_head_fingerprint()


def test_0018_completes_a_partial_run_that_created_only_webhook_events(
    at_0016: Settings,
) -> None:
    """webhook_events without its parent table is finished, not skipped."""
    config = _config(at_0016)
    command.upgrade(config, HEAD)
    engine = _engine(at_0016)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE platform_subscriptions"))
        connection.execute(text("UPDATE alembic_version SET version_num = '0016_shared_contracts'"))
    engine.dispose()

    command.upgrade(config, HEAD)

    assert _tables(at_0016) >= WEBHOOK_TABLES
    assert _snapshot(at_0016)["fingerprint"] == _committed_head_fingerprint()


def test_postgres_upgrade_creates_both_tables_with_indexes(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "pg")
    config = _config(settings)
    config.set_main_option("sqlalchemy.url", "postgresql://runway@localhost/runway")
    buffer = io.StringIO()
    config.output_buffer = buffer
    command.upgrade(config, "0016_shared_contracts:head", sql=True)
    sql = buffer.getvalue()

    assert "_alembic_tmp" not in sql
    assert "CREATE TABLE platform_subscriptions (" in sql
    assert "CREATE TABLE webhook_events (" in sql
    assert "CREATE INDEX ix_webhook_events_provider_received" in sql
    assert "CREATE INDEX ix_webhook_events_processed_at" in sql
    assert "ON DELETE SET NULL" in sql
    assert "ON DELETE CASCADE" in sql


def test_webhook_events_accepts_null_dedupe_keys(at_0016: Settings) -> None:
    """Unverified events carry no dedupe key, so several NULLs must coexist."""
    config = _config(at_0016)
    command.upgrade(config, HEAD)
    with sqlite3.connect(at_0016.database_path) as connection:
        for _ in range(3):
            connection.execute(
                "INSERT INTO webhook_events (provider, received_at, signature_ok, body) "
                "VALUES ('tiktok', CURRENT_TIMESTAMP, 0, X'00')"
            )
        assert connection.execute("SELECT count(*) FROM webhook_events").fetchone() == (3,)
        assert connection.execute(
            "SELECT count(*) FROM webhook_events WHERE dedupe_key IS NULL"
        ).fetchone() == (3,)
        # And the same key cannot be stored twice.
        with pytest.raises(sqlite3.IntegrityError):
            for _ in range(2):
                connection.execute(
                    "INSERT INTO webhook_events (provider, received_at, signature_ok, "
                    "dedupe_key, body) VALUES ('tiktok', CURRENT_TIMESTAMP, 1, 'dup', X'00')"
                )
