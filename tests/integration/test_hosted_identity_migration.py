"""0016h_hosted_identity: additive tables with a tested downgrade."""

from __future__ import annotations

from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from runway.config import Settings
from runway.db.bootstrap import alembic_config

NEW_TABLES = {"workspace_identity_links", "identity_webhook_deliveries", "workspace_limits"}


def _config(settings: Settings) -> Config:
    settings.ensure_directories()
    return alembic_config(settings)


def _counts(settings: Settings) -> dict[str, int]:
    engine = create_engine(settings.database_url)
    try:
        with engine.connect() as connection:
            return {
                table: connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one()
                for table in ("workspaces", "users", "workspace_members", "channels")
            }
    finally:
        engine.dispose()


def test_upgrade_and_downgrade_keep_existing_rows(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    config = _config(settings)
    # 0001 builds a fresh database from the models, hosted tables included; the
    # downgrade leaves the database as one written before 0016h existed.
    command.upgrade(config, "head")
    command.downgrade(config, "0013_workspaces")
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (external_id, email, display_name, created_at) "
                "VALUES ('user_a', 'a@example.test', 'A', '2026-11-22T10:00:00')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO workspace_members (workspace_id, user_id, role, created_at) "
                "VALUES (1, 1, 'owner', '2026-11-22T10:00:00')"
            )
        )
        assert NEW_TABLES.isdisjoint(inspect(connection).get_table_names())
    engine.dispose()
    before = _counts(settings)

    command.upgrade(config, "head")
    engine = create_engine(settings.database_url)
    with engine.connect() as connection:
        assert set(inspect(connection).get_table_names()) >= NEW_TABLES
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert version == "0016h_hosted_identity"
        # The kill switch defaults to off for rows written without it.
        connection.execute(
            text("INSERT INTO workspace_limits (workspace_id, updated_at) VALUES (1, '2026-11-22')")
        )
        assert (
            connection.execute(
                text("SELECT publishing_disabled FROM workspace_limits")
            ).scalar_one()
            == 0
        )
        connection.rollback()
    engine.dispose()
    assert _counts(settings) == before

    command.downgrade(config, "0013_workspaces")
    engine = create_engine(settings.database_url)
    with engine.connect() as connection:
        assert NEW_TABLES.isdisjoint(inspect(connection).get_table_names())
    engine.dispose()
    assert _counts(settings) == before

    command.upgrade(config, "head")
    assert _counts(settings) == before


def test_fresh_and_upgraded_databases_have_the_same_new_tables(tmp_path: Path) -> None:
    fresh = Settings(data_dir=tmp_path / "fresh")
    command.upgrade(_config(fresh), "head")
    upgraded = Settings(data_dir=tmp_path / "upgraded")
    config = _config(upgraded)
    command.upgrade(config, "head")
    # Back to 0013 and forward again: the tables now come from 0016h's DDL.
    command.downgrade(config, "0013_workspaces")
    command.upgrade(config, "head")

    def describe(settings: Settings) -> dict[str, object]:
        engine = create_engine(settings.database_url)
        try:
            inspector = inspect(engine)
            return {
                table: (
                    sorted(
                        (c["name"], str(c["type"]), c["nullable"])
                        for c in inspector.get_columns(table)
                    ),
                    sorted(str(u["column_names"]) for u in inspector.get_unique_constraints(table)),
                    sorted(str(i["column_names"]) for i in inspector.get_indexes(table)),
                    sorted(str(f["referred_table"]) for f in inspector.get_foreign_keys(table)),
                )
                for table in sorted(NEW_TABLES)
            }
        finally:
            engine.dispose()

    assert describe(fresh) == describe(upgraded)
