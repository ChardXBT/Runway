"""Migrations 0013-0015: workspaces, workspace columns and the job queue.

A database built at 0012 the way production was (migration by migration) is
reproduced by upgrading to head and downgrading to 0012: revision 0001 builds
from the current models, so a fresh `upgrade 0012` would already contain the
new columns. The downgraded schema must equal the committed 0012 contract.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from alembic import command
from runway.config import Settings
from runway.db.bootstrap import alembic_config
from runway.db.schema_contract import schema_snapshot

# Fingerprint of docs/schema/intelligence-data-flywheel.json at 0012_creator_platforms.
SCHEMA_0012_FINGERPRINT = "b70dadf3387170f628f8f1f280e0ac05b7063d60c99d379fe249864add4732be"
HEAD = "0016_shared_contracts"
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
}
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
    for revision in ("0013_workspaces", "0014_workspace_columns", "0015_publish_jobs", HEAD):
        command.upgrade(config, revision)
    for revision in (
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
