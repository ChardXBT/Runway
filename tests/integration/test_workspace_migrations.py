"""Migrations 0013-0015: workspaces, workspace columns and the job queue.

A database built at 0012 the way production was (migration by migration) is
reproduced by upgrading to head and downgrading to 0012: revision 0001 builds
from the current models, so a fresh `upgrade 0012` would already contain the
new columns. The downgraded schema must equal the committed 0012 contract.
"""

from __future__ import annotations

import importlib.util
import io
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from alembic import command
from runway.config import Settings
from runway.db.bootstrap import alembic_config, migration_head
from runway.db.schema_contract import load_schema_snapshot, schema_snapshot

# Fingerprint of docs/schema/intelligence-data-flywheel.json at 0012_creator_platforms.
SCHEMA_0012_FINGERPRINT = "b70dadf3387170f628f8f1f280e0ac05b7063d60c99d379fe249864add4732be"
HEAD = migration_head(Settings())
SCOPED_TABLES = (
    "channels",
    "connected_accounts",
    "oauth_states",
    "content_groups",
    "platform_content",
    "content_metric_snapshots",
    "account_metric_snapshots",
    "account_metric_days",
    "comment_records",
    "reply_drafts",
    "platform_actions",
    "sync_runs",
    "sync_cursors",
    "content_drafts",
    "draft_targets",
)
NEW_TABLES = {
    "workspaces",
    "users",
    "workspace_members",
    "api_keys",
    "audit_log",
    "publish_jobs",
    "idempotency_keys",
    "events",
    "workspace_policies",
    "api_key_release_grants",
    "webhook_endpoints",
    "webhook_deliveries",
}
AGENT_TABLES = (
    "workspace_policies",
    "api_key_release_grants",
    "webhook_endpoints",
    "webhook_deliveries",
)
NOW = "2026-11-01 10:00:00.000000"

FIXTURE_ROWS = [
    "INSERT INTO channels (id, name, handle, youtube_channel_id, timezone, default_post_time, "
    "planning_horizon_days, duplicate_window_days, created_at, updated_at) VALUES "
    f"(1, 'Northwind', '@northwind', NULL, 'UTC', '09:00', 0, 180, '{NOW}', '{NOW}')",
    *[
        "INSERT INTO connected_accounts (id, platform, external_account_id, handle, status, "
        "granted_scopes_json, token_ref, connected_at, metadata_json, created_at, updated_at) "
        f"VALUES ({i}, '{platform}', 'ext-{i}', 'h{i}', 'connected', '[]', 'ref{i}', "
        f"'{NOW}', '{{}}', '{NOW}', '{NOW}')"
        for i, platform in ((1, "youtube"), (2, "instagram"), (3, "tiktok"))
    ],
    "INSERT INTO oauth_states (id, platform, state, code_verifier, redirect_uri, created_at, "
    f"expires_at, consumed_at) VALUES (1, 'youtube', 's1', NULL, 'http://127.0.0.1/cb', "
    f"'{NOW}', '{NOW}', NULL)",
    f"INSERT INTO content_groups (id, title, note, created_at) VALUES (1, 'Group', NULL, '{NOW}')",
    *[
        "INSERT INTO platform_content (id, account_id, platform, external_id, content_type, "
        "metrics_json, content_group_id, first_seen_at, last_seen_at, raw_json, comment_count) "
        f"VALUES ({i}, 1, 'youtube', 'vid-{i}', 'video', '{{}}', 1, '{NOW}', '{NOW}', '{{}}', 2)"
        for i in (1, 2)
    ],
    "INSERT INTO content_metric_snapshots (id, content_id, captured_at, metrics_json) VALUES "
    f"(1, 1, '{NOW}', '{{}}'), (2, 2, '{NOW}', '{{}}')",
    "INSERT INTO account_metric_snapshots (id, account_id, captured_at, followers, metrics_json) "
    f"VALUES (1, 1, '{NOW}', 10, '{{}}')",
    "INSERT INTO account_metric_days (id, account_id, day, metric, value, source, fetched_at) "
    f"VALUES (1, 1, '2026-10-31', 'views', 5.0, 'youtube_analytics', '{NOW}')",
    *[
        "INSERT INTO comment_records (id, account_id, platform, external_id, content_id, "
        "author_is_owner, text, status, has_owner_reply, raw_json, first_seen_at, last_seen_at) "
        f"VALUES ({i}, 1, 'youtube', 'c-{i}', 1, 0, 'hello {i}', 'new', 0, '{{}}', '{NOW}', "
        f"'{NOW}')"
        for i in (1, 2)
    ],
    f"INSERT INTO reply_drafts (id, comment_id, text, updated_at) VALUES (1, 1, 'thanks', '{NOW}')",
    "INSERT INTO platform_actions (id, account_id, action_type, target_type, target_id, state, "
    "request_json, response_json, created_at) VALUES "
    f"(1, 1, 'comment_reply', 'comment', 1, 'completed', '{{}}', '{{}}', '{NOW}')",
    "INSERT INTO sync_runs (id, account_id, trigger, status, started_at, steps_json, "
    "items_fetched, quota_units_estimate) VALUES "
    f"(1, 1, 'manual', 'completed', '{NOW}', '[]', 3, 4)",
    "INSERT INTO sync_cursors (id, account_id, scope, cursor_json, updated_at) VALUES "
    f"(1, 1, 'content', '{{}}', '{NOW}')",
    "INSERT INTO content_drafts (id, title, body, status, created_at, updated_at) VALUES "
    f"(1, 'Draft', 'body', 'draft', '{NOW}', '{NOW}'), "
    f"(2, 'Sent', 'body', 'published', '{NOW}', '{NOW}')",
    "INSERT INTO draft_targets (id, draft_id, platform, account_id, settings_json, status) VALUES "
    "(1, 1, 'youtube', 1, '{}', 'pending'), (2, 2, 'instagram', 2, '{}', 'published')",
]


def _config(settings: Settings):  # type: ignore[no-untyped-def]
    return alembic_config(settings)


def _engine(settings: Settings) -> Engine:
    return create_engine(settings.database_url)


def _row_counts(settings: Settings) -> dict[str, int]:
    engine = _engine(settings)
    try:
        tables = [name for name in inspect(engine).get_table_names() if name != "alembic_version"]
        with engine.connect() as connection:
            return {
                table: int(connection.execute(text(f'SELECT count(*) FROM "{table}"')).scalar_one())
                for table in tables
            }
    finally:
        engine.dispose()


def _snapshot(settings: Settings) -> dict[str, object]:
    engine = _engine(settings)
    try:
        return schema_snapshot(engine)
    finally:
        engine.dispose()


def _foreign_key_violations(settings: Settings) -> list[tuple[object, ...]]:
    with sqlite3.connect(settings.database_path) as connection:
        rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    connection.close()
    return [tuple(row) for row in rows]


@pytest.fixture()
def at_0012(tmp_path: Path) -> Settings:
    """A database with the real 0012 schema and fixture rows in every platform table."""
    settings = Settings(data_dir=tmp_path / "data", channel_name="Northwind Studio")
    settings.ensure_directories()
    config = _config(settings)
    command.upgrade(config, "head")
    command.downgrade(config, "0012_creator_platforms")
    engine = _engine(settings)
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM channels"))
        for statement in FIXTURE_ROWS:
            connection.execute(text(statement))
    engine.dispose()
    return settings


def test_downgrade_to_0012_restores_the_committed_0012_schema(at_0012: Settings) -> None:
    snapshot = _snapshot(at_0012)
    assert snapshot["migration"] == "0012_creator_platforms"
    assert snapshot["fingerprint"] == SCHEMA_0012_FINGERPRINT
    assert NEW_TABLES.isdisjoint(inspect(_engine(at_0012)).get_table_names())


def test_upgrade_from_0012_with_rows_keeps_every_row_and_backfills_workspace_1(
    at_0012: Settings,
) -> None:
    before = _row_counts(at_0012)
    assert all(before[table] > 0 for table in SCOPED_TABLES)

    command.upgrade(_config(at_0012), "head")

    after = _row_counts(at_0012)
    assert {table: after[table] for table in before} == before
    assert after["workspaces"] == 1
    assert after["publish_jobs"] == 0
    assert after["events"] == after["idempotency_keys"] == 0
    engine = _engine(at_0012)
    with engine.connect() as connection:
        workspace = connection.execute(text("SELECT id, slug, name FROM workspaces")).one()
        assert tuple(workspace) == (1, "northwind-studio", "Northwind Studio")
        for table in SCOPED_TABLES:
            values = connection.execute(text(f"SELECT DISTINCT workspace_id FROM {table}")).all()
            assert values == [(1,)], table
        # Existing ids and values survive the batch table rebuilds.
        assert connection.execute(
            text("SELECT id, status FROM draft_targets ORDER BY id")
        ).all() == [(1, "pending"), (2, "published")]
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one() == (
            HEAD
        )
    inspector = inspect(engine)
    for table in SCOPED_TABLES:
        column = next(c for c in inspector.get_columns(table) if c["name"] == "workspace_id")
        assert column["nullable"] is False
        assert str(column["default"]).strip("'\"") == "1"
        assert any(
            fk["referred_table"] == "workspaces" and fk["constrained_columns"] == ["workspace_id"]
            for fk in inspector.get_foreign_keys(table)
        ), table
    unique = {
        constraint["name"]: constraint["column_names"]
        for constraint in inspector.get_unique_constraints("connected_accounts")
    }
    assert unique["uq_connected_account_external"] == [
        "workspace_id",
        "platform",
        "external_account_id",
    ]
    engine.dispose()
    assert _foreign_key_violations(at_0012) == []


def test_downgrade_after_upgrade_keeps_rows_and_restores_0012(at_0012: Settings) -> None:
    before = _row_counts(at_0012)
    config = _config(at_0012)
    command.upgrade(config, "head")
    command.downgrade(config, "0012_creator_platforms")

    assert _row_counts(at_0012) == before
    assert _snapshot(at_0012)["fingerprint"] == SCHEMA_0012_FINGERPRINT
    assert _foreign_key_violations(at_0012) == []


def test_each_revision_downgrades_one_step_at_a_time(at_0012: Settings) -> None:
    before = _row_counts(at_0012)
    config = _config(at_0012)
    for revision in (
        "0013_workspaces",
        "0014_workspace_columns",
        "0015_publish_jobs",
        "0016_shared_contracts",
        HEAD,
    ):
        command.upgrade(config, revision)
    for revision in (
        "0016_shared_contracts",
        "0015_publish_jobs",
        "0014_workspace_columns",
        "0013_workspaces",
        "0012_creator_platforms",
    ):
        command.downgrade(config, revision)
        counts = _row_counts(at_0012)
        assert {table: counts[table] for table in before} == before, revision
    assert _snapshot(at_0012)["fingerprint"] == SCHEMA_0012_FINGERPRINT


def test_the_same_platform_account_may_be_connected_once_per_workspace(
    at_0012: Settings,
) -> None:
    command.upgrade(_config(at_0012), "head")
    engine = _engine(at_0012)
    insert = (
        "INSERT INTO connected_accounts (workspace_id, platform, external_account_id, status, "
        "granted_scopes_json, connected_at, metadata_json, created_at, updated_at) VALUES "
        f"(:workspace, 'youtube', 'ext-1', 'connected', '[]', '{NOW}', '{{}}', '{NOW}', '{NOW}')"
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, slug, name, created_at) "
                f"VALUES (2, 'second', 'Second', '{NOW}')"
            )
        )
        connection.execute(text(insert), {"workspace": 2})
    with pytest.raises(IntegrityError), engine.begin() as connection:
        connection.execute(text(insert), {"workspace": 1})
    engine.dispose()


def test_fresh_database_upgrades_and_downgrades_cleanly(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "fresh")
    settings.ensure_directories()
    config = _config(settings)
    command.upgrade(config, "head")
    fresh = _snapshot(settings)
    assert fresh["migration"] == HEAD
    assert _row_counts(settings)["workspaces"] == 1

    command.downgrade(config, "0012_creator_platforms")
    assert _snapshot(settings)["fingerprint"] == SCHEMA_0012_FINGERPRINT
    command.upgrade(config, "head")
    rebuilt = _snapshot(settings)
    # The model-built schema (0001 create_all) and the migrated one are identical.
    assert rebuilt["fingerprint"] == fresh["fingerprint"]
    assert _row_counts(settings)["workspaces"] == 1


def _events_ddl(settings: Settings) -> str:
    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'events'"
        ).fetchone()
    connection.close()
    return str(row[0]) if row else ""


def test_0016_adds_and_removes_only_the_shared_contract_tables(at_0012: Settings) -> None:
    config = _config(at_0012)
    command.upgrade(config, "0015_publish_jobs")
    before = _row_counts(at_0012)
    snapshot_0015 = _snapshot(at_0012)

    command.upgrade(config, "0016_shared_contracts")
    after = _row_counts(at_0012)
    assert {table: after[table] for table in before} == before
    assert after["events"] == after["idempotency_keys"] == 0
    # Event ids never repeat (Last-Event-ID resume depends on it).
    assert "AUTOINCREMENT" in _events_ddl(at_0012).upper()
    inspector = inspect(_engine(at_0012))
    unique = {
        row["name"]: row["column_names"]
        for row in inspector.get_unique_constraints("idempotency_keys")
    }
    assert unique["uq_idempotency_key"] == ["workspace_id", "principal_id", "key"]

    command.downgrade(config, "0015_publish_jobs")
    assert _row_counts(at_0012) == before
    assert _snapshot(at_0012)["fingerprint"] == snapshot_0015["fingerprint"]


def test_a_fresh_database_also_gets_autoincrement_event_ids(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "fresh-events")
    settings.ensure_directories()
    command.upgrade(_config(settings), "head")
    assert "AUTOINCREMENT" in _events_ddl(settings).upper()


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


def test_a_crash_mid_0014_rolls_the_revision_back_and_a_rerun_succeeds(
    at_0012: Settings,
) -> None:
    before = _row_counts(at_0012)
    config = _config(at_0012)

    # Fails after channels and connected_accounts were already rebuilt.
    with (
        _crash_on("CREATE TABLE _alembic_tmp_platform_content") as hit,
        pytest.raises(InjectedCrash),
    ):
        command.upgrade(config, "head")
    assert hit, "the crash point was never reached"

    # 0013 committed on its own; every statement of 0014 was rolled back.
    assert _version(at_0012) == "0013_workspaces"
    assert not {name for name in _tables(at_0012) if name.startswith("_alembic_tmp_")}
    engine = _engine(at_0012)
    for table in ("channels", "connected_accounts", "platform_content"):
        assert "workspace_id" not in {c["name"] for c in inspect(engine).get_columns(table)}
    engine.dispose()
    counts = _row_counts(at_0012)
    assert {table: counts[table] for table in before} == before

    command.upgrade(config, "head")

    assert _version(at_0012) == HEAD
    after = _row_counts(at_0012)
    assert {table: after[table] for table in before} == before
    assert _snapshot(at_0012)["fingerprint"] == _committed_head_fingerprint()
    assert _foreign_key_violations(at_0012) == []


def test_a_crash_mid_0013_leaves_no_partial_tables_and_a_rerun_succeeds(
    at_0012: Settings,
) -> None:
    before = _row_counts(at_0012)
    config = _config(at_0012)

    with _crash_on("CREATE TABLE api_keys") as hit, pytest.raises(InjectedCrash):
        command.upgrade(config, "head")
    assert hit

    assert _version(at_0012) == "0012_creator_platforms"
    assert {"workspaces", "users", "workspace_members"}.isdisjoint(_tables(at_0012))

    command.upgrade(config, "head")

    assert _version(at_0012) == HEAD
    after = _row_counts(at_0012)
    assert {table: after[table] for table in before} == before
    assert after["workspaces"] == 1
    assert _snapshot(at_0012)["fingerprint"] == _committed_head_fingerprint()


def test_a_leftover_batch_table_from_an_old_crash_is_cleared(at_0012: Settings) -> None:
    """The state a pre-fix, non-transactional crash left on disk: 0013 plus a temp table."""
    before = _row_counts(at_0012)
    config = _config(at_0012)
    command.upgrade(config, "0013_workspaces")
    engine = _engine(at_0012)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE _alembic_tmp_channels (id INTEGER PRIMARY KEY)"))
    engine.dispose()

    command.upgrade(config, "head")

    assert "_alembic_tmp_channels" not in _tables(at_0012)
    after = _row_counts(at_0012)
    assert {table: after[table] for table in before} == before
    assert _snapshot(at_0012)["fingerprint"] == _committed_head_fingerprint()


def test_0013_completes_a_partial_run_that_created_only_workspaces(at_0012: Settings) -> None:
    config = _config(at_0012)
    command.upgrade(config, "0013_workspaces")
    engine = _engine(at_0012)
    with engine.begin() as connection:
        # What a pre-fix crash right after CREATE TABLE workspaces looked like.
        for table in ("audit_log", "api_keys", "workspace_members", "users"):
            connection.execute(text(f"DROP TABLE {table}"))
        connection.execute(
            text("UPDATE alembic_version SET version_num = '0012_creator_platforms'")
        )
    engine.dispose()

    command.upgrade(config, "head")

    assert {"users", "workspace_members", "api_keys", "audit_log"} <= _tables(at_0012)
    assert _row_counts(at_0012)["workspaces"] == 1
    assert _snapshot(at_0012)["fingerprint"] == _committed_head_fingerprint()


# --------------------------------------------------------------- Postgres SQL


def _offline_postgres_sql(tmp_path: Path, revisions: str) -> str:
    settings = Settings(data_dir=tmp_path / "pg")
    config = _config(settings)
    config.set_main_option("sqlalchemy.url", "postgresql://runway@localhost/runway")
    buffer = io.StringIO()
    config.output_buffer = buffer
    start = revisions.partition(":")[0]
    if start == HEAD:
        command.downgrade(config, revisions, sql=True)
    else:
        command.upgrade(config, revisions, sql=True)
    return buffer.getvalue()


def test_postgres_upgrade_alters_in_place_and_advances_the_workspace_sequence(
    tmp_path: Path,
) -> None:
    sql = _offline_postgres_sql(tmp_path, f"0012_creator_platforms:{HEAD}")

    # recreate="auto": plain ALTERs, never copy/drop/rename of referenced tables.
    assert "_alembic_tmp" not in sql
    assert "DROP TABLE channels" not in sql
    assert "ALTER TABLE channels ADD COLUMN workspace_id INTEGER DEFAULT 1 NOT NULL" in sql
    assert "ALTER TABLE connected_accounts DROP CONSTRAINT uq_connected_account_external" in sql
    assert "SELECT setval(pg_get_serial_sequence('workspaces', 'id')" in sql
    for table in ("users", "workspace_members", "api_keys", "audit_log", "publish_jobs"):
        assert f"CREATE TABLE {table} (" in sql
    assert "CREATE TABLE idempotency_keys (" in sql and "CREATE TABLE events (" in sql
    # Every revision is its own transaction.
    assert sql.count("BEGIN;") == sql.count("COMMIT;") >= 4


def test_postgres_downgrade_alters_in_place(tmp_path: Path) -> None:
    sql = _offline_postgres_sql(tmp_path, f"{HEAD}:0012_creator_platforms")
    assert "_alembic_tmp" not in sql
    assert "ALTER TABLE channels DROP COLUMN workspace_id" in sql
    assert "DROP TABLE workspaces" in sql


def test_postgres_full_chain_renders_only_portable_sql(tmp_path: Path) -> None:
    """Every revision, base->head and head->base, emits SQL Postgres accepts.

    Offline there is nothing to inspect: each revision must still emit its DDL
    instead of calling `sa.inspect` on a missing connection, and nothing in the
    chain may produce SQLite-only syntax."""
    up = _offline_postgres_sql(tmp_path, f"base:{HEAD}")

    # The whole chain ran: the final version stamp is the head revision.
    assert f"UPDATE alembic_version SET version_num='{HEAD}'" in up
    # No SQLite-only syntax survives into the Postgres script.
    assert "PRAGMA" not in up
    assert "AUTOINCREMENT" not in up
    assert "sqlite" not in up.lower()
    # Batch mode uses recreate="auto": no move-and-copy on Postgres.
    assert "_alembic_tmp" not in up
    # Boolean server defaults render as real booleans, never SQLite's 0/1.
    for bad in (
        "BOOLEAN DEFAULT 0",
        "BOOLEAN DEFAULT 1",
        "BOOLEAN DEFAULT '0'",
        "BOOLEAN DEFAULT '1'",
    ):
        assert bad not in up
    assert "BOOLEAN DEFAULT false" in up

    down = _offline_postgres_sql(tmp_path, f"{HEAD}:base")
    assert "PRAGMA" not in down
    assert "_alembic_tmp" not in down
    # 0001 cannot sort the proposals<->caption_slates cycle for DROP: it emits
    # CASCADE drops on Postgres instead of metadata.drop_all().
    assert 'DROP TABLE "proposals" CASCADE' in down
    # The whole chain ran back: head's own downgrade and the removal of 0001.
    assert f"WHERE alembic_version.version_num = '{HEAD}'" in down
    assert "DELETE FROM alembic_version WHERE alembic_version.version_num = '0001_initial'" in down


def test_0019_adds_and_removes_only_the_agent_interface_tables(at_0012: Settings) -> None:
    config = _config(at_0012)
    command.upgrade(config, "0016_shared_contracts")
    before = _row_counts(at_0012)
    snapshot_0016 = _snapshot(at_0012)
    assert set(AGENT_TABLES).isdisjoint(inspect(_engine(at_0012)).get_table_names())

    command.upgrade(config, "0019_agent_interface")
    after = _row_counts(at_0012)
    assert {table: after[table] for table in before} == before
    assert all(after[table] == 0 for table in AGENT_TABLES)
    inspector = inspect(_engine(at_0012))
    unique = {
        row["name"]: row["column_names"]
        for row in inspector.get_unique_constraints("webhook_deliveries")
    }
    assert unique["uq_webhook_delivery_event"] == ["endpoint_id", "event_id"]

    command.downgrade(config, "0016_shared_contracts")
    assert _row_counts(at_0012) == before
    assert _snapshot(at_0012)["fingerprint"] == snapshot_0016["fingerprint"]


def _load_0019() -> Any:
    spec = importlib.util.spec_from_file_location(
        "migration_0019",
        Path(__file__).parents[2] / "alembic" / "versions" / "0019_agent_interface.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_0019_emits_plain_ddl_and_drops_in_offline_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Offline (`--sql`) there is no database to inspect: the revision emits
    unconditional DDL instead of calling the inspector."""
    migration = _load_0019()
    created: list[str] = []
    dropped: list[str] = []
    monkeypatch.setattr(migration.context, "is_offline_mode", lambda: True)
    monkeypatch.setattr(
        migration.op,
        "create_table",
        lambda name, *_args, **_kwargs: created.append(str(name)),
    )
    monkeypatch.setattr(migration.op, "create_index", lambda *_a, **_k: None)
    monkeypatch.setattr(migration.op, "drop_table", lambda name: dropped.append(str(name)))
    migration.upgrade()
    assert set(created) == set(migration.NEW_TABLES)
    migration.downgrade()
    assert dropped == list(migration.NEW_TABLES)


def test_0019_downgrade_leaves_a_table_it_did_not_create(tmp_path: Path) -> None:
    """A same-named table with a foreign shape is not this revision's to drop."""
    settings = Settings(data_dir=tmp_path / "foreign")
    settings.ensure_directories()
    config = _config(settings)
    command.upgrade(config, "0016_shared_contracts")
    engine = _engine(settings)
    # Stand-in for a table another revision (or a hand fix) made first: same
    # name as an agent table but none of this revision's signature columns.
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE webhook_endpoints"))
        connection.execute(
            text("CREATE TABLE webhook_endpoints (id INTEGER PRIMARY KEY, note TEXT)")
        )
        connection.execute(text("INSERT INTO webhook_endpoints (id, note) VALUES (1, 'keep')"))
    command.upgrade(config, "head")
    command.downgrade(config, "0016_shared_contracts")
    names = set(inspect(engine).get_table_names())
    assert "webhook_endpoints" in names  # foreign shape: preserved
    assert "webhook_deliveries" not in names  # created by 0019: dropped
    assert "workspace_policies" not in names
    assert "api_key_release_grants" not in names
    with engine.connect() as connection:
        assert connection.execute(text("SELECT note FROM webhook_endpoints")).all() == [("keep",)]
    engine.dispose()


def test_0020_offline_downgrade_emits_both_oauth_states_drops(tmp_path: Path) -> None:
    """`--sql` mode cannot inspect; the drops must be emitted unconditionally."""
    settings = Settings(data_dir=tmp_path / "pg")
    config = _config(settings)
    config.set_main_option("sqlalchemy.url", "postgresql://runway@localhost/runway")
    buffer = io.StringIO()
    config.output_buffer = buffer
    command.downgrade(config, "0020_youtube_publishing:0019_agent_interface", sql=True)
    sql = buffer.getvalue()
    assert "ALTER TABLE oauth_states DROP COLUMN account_id" in sql
    assert "ALTER TABLE oauth_states DROP COLUMN purpose" in sql
