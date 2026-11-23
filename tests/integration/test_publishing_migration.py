"""Migration 0017_publishing_engine: posts/targets columns with backfill, media and ledger tables.

The previous head is reproduced the way production reaches it (upgrade to head
then downgrade, because 0001 builds from the current models), seeded with
rows, upgraded and downgraded; row counts and values must survive both ways.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from alembic import command
from runway.config import Settings
from runway.db.bootstrap import alembic_config
from runway.db.schema_contract import schema_snapshot

REVISION = "0017_publishing_engine"
PREVIOUS = "0016h_hosted_identity"
NOW = "2026-11-01 10:00:00.000000"
NEW_TABLES = {
    "media_assets_v2",
    "media_uploads",
    "draft_media",
    "target_attempts",
    "account_publishers",
    "publishing_controls",
}
FIXTURE_ROWS = [
    *[
        "INSERT INTO connected_accounts (id, platform, external_account_id, handle, status, "
        "granted_scopes_json, token_ref, connected_at, metadata_json, created_at, updated_at) "
        f"VALUES ({i}, '{platform}', 'ext-{i}', 'h{i}', 'connected', '[]', 'ref{i}', "
        f"'{NOW}', '{{}}', '{NOW}', '{NOW}')"
        for i, platform in ((1, "youtube"), (2, "instagram"), (3, "tiktok"))
    ],
    "INSERT INTO content_drafts (id, title, body, status, scheduled_for, created_at, updated_at) "
    f"VALUES (1, 'Draft', 'body', 'scheduled', '2026-12-01 09:00:00.000000', '{NOW}', '{NOW}'), "
    f"(2, 'Sent', 'body', 'published', NULL, '{NOW}', '{NOW}')",
    "INSERT INTO draft_targets (id, draft_id, platform, account_id, settings_json, status) VALUES "
    "(1, 1, 'youtube', 1, '{}', 'pending'), (2, 2, 'instagram', 2, '{}', 'published'), "
    "(3, 1, 'tiktok', 3, '{}', 'pending')",
]


def _engine(settings: Settings):  # type: ignore[no-untyped-def]
    return create_engine(settings.database_url)


def _counts(settings: Settings) -> dict[str, int]:
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


def _fingerprint(settings: Settings) -> str:
    engine = _engine(settings)
    try:
        return str(schema_snapshot(engine)["fingerprint"])
    finally:
        engine.dispose()


def _migrated_previous(tmp_path: Path, name: str) -> Settings:
    """A database at the previous head reached by migrations from 0012 (production's path)."""
    settings = Settings(data_dir=tmp_path / name)
    settings.ensure_directories()
    config = alembic_config(settings)
    command.upgrade(config, "head")
    command.downgrade(config, "0012_creator_platforms")
    command.upgrade(config, PREVIOUS)
    return settings


@pytest.fixture()
def at_previous(tmp_path: Path) -> Settings:
    settings = _migrated_previous(tmp_path, "data")
    engine = _engine(settings)
    with engine.begin() as connection:
        for statement in FIXTURE_ROWS:
            connection.execute(text(statement))
    engine.dispose()
    return settings


def test_revision_follows_the_shared_contracts_head(at_previous: Settings) -> None:
    script = ScriptDirectory.from_config(alembic_config(at_previous))
    revision = script.get_revision(REVISION)
    assert revision is not None and revision.down_revision == PREVIOUS


def test_upgrade_keeps_rows_and_backfills_targets(at_previous: Settings) -> None:
    before = _counts(at_previous)
    command.upgrade(alembic_config(at_previous), REVISION)
    after = _counts(at_previous)
    assert {table: after[table] for table in before} == before
    assert set(after) >= NEW_TABLES
    assert all(after[table] == 0 for table in NEW_TABLES)
    engine = _engine(at_previous)
    with engine.connect() as connection:
        targets = connection.execute(
            text(
                "SELECT id, status, state, post_type, planned_at, release_state, version, "
                "workspace_id FROM draft_targets ORDER BY id"
            )
        ).all()
        posts = connection.execute(
            text("SELECT id, source, ai_generated, version FROM content_drafts ORDER BY id")
        ).all()
    engine.dispose()
    assert [tuple(row) for row in targets] == [
        (1, "pending", "pending", "youtube_video", "2026-12-01 09:00:00.000000", "none", 1, 1),
        (2, "published", "published", "instagram_image", None, "none", 1, 1),
        (3, "pending", "pending", "tiktok_video", "2026-12-01 09:00:00.000000", "none", 1, 1),
    ]
    assert [tuple(row) for row in posts] == [(1, "human", 0, 1), (2, "human", 0, 1)]


def test_downgrade_restores_the_previous_schema_and_keeps_rows(
    at_previous: Settings, tmp_path: Path
) -> None:
    before = _counts(at_previous)
    previous_fingerprint = _fingerprint(at_previous)
    config = alembic_config(at_previous)
    command.upgrade(config, REVISION)
    command.downgrade(config, PREVIOUS)
    assert _counts(at_previous) == before
    assert _fingerprint(at_previous) == previous_fingerprint
    # And a second upgrade after the downgrade works (no leftovers).
    command.upgrade(config, REVISION)
    assert {table: count for table, count in _counts(at_previous).items() if table in before} == (
        before
    )


def test_migrated_schema_equals_the_model_schema(tmp_path: Path) -> None:
    fresh = Settings(data_dir=tmp_path / "fresh")
    fresh.ensure_directories()
    command.upgrade(alembic_config(fresh), "head")
    migrated = _migrated_previous(tmp_path, "migrated")
    command.upgrade(alembic_config(migrated), "head")
    assert _fingerprint(migrated) == _fingerprint(fresh)


def test_an_interrupted_upgrade_can_be_run_again(at_previous: Settings) -> None:
    engine = _engine(at_previous)
    with engine.begin() as connection:
        # A crash after the first column and table were created, before the rest.
        connection.execute(
            text(
                "ALTER TABLE content_drafts ADD COLUMN source VARCHAR(20) DEFAULT 'human' NOT NULL"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE publishing_controls (workspace_id INTEGER NOT NULL, paused BOOLEAN "
                "DEFAULT 0 NOT NULL, reason TEXT, updated_by VARCHAR(200), updated_at DATETIME "
                "NOT NULL, PRIMARY KEY (workspace_id), CONSTRAINT fk_publishing_controls_workspace "
                "FOREIGN KEY(workspace_id) REFERENCES workspaces (id))"
            )
        )
    engine.dispose()
    command.upgrade(alembic_config(at_previous), REVISION)
    assert set(_counts(at_previous)) >= NEW_TABLES
