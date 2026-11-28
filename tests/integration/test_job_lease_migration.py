from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from runway.config import Settings

LEASE_COLUMNS = {"owner_token", "heartbeat_at", "lease_expires_at", "lease_version"}
JOB_TABLES = ("search_runs", "generation_runs", "sync_runs")


def _config(settings: Settings) -> Config:
    config = Config(str(settings.project_root / "alembic.ini"))
    config.set_main_option("script_location", str(settings.project_root / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    return config


def _seed_pre_0013_schema(database_url: str) -> None:
    engine = create_engine(database_url)
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        connection.execute(
            text("INSERT INTO alembic_version (version_num) VALUES ('0012_creator_platforms')")
        )
        connection.execute(
            text(
                "CREATE TABLE search_runs ("
                "id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, "
                "style_profile_id INTEGER, query_plan_json TEXT NOT NULL, "
                "provider VARCHAR(80) NOT NULL, status VARCHAR(40) NOT NULL, "
                "started_at DATETIME NOT NULL, completed_at DATETIME, "
                "result_count INTEGER NOT NULL, error_summary TEXT)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE generation_runs ("
                "id INTEGER PRIMARY KEY, channel_id INTEGER NOT NULL, "
                "style_profile_id INTEGER, start_date DATE NOT NULL, days INTEGER NOT NULL, "
                "status VARCHAR(40) NOT NULL, started_at DATETIME NOT NULL, "
                "completed_at DATETIME, error_summary TEXT, "
                "selection_diagnostics_json TEXT NOT NULL)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE sync_runs ("
                "id INTEGER PRIMARY KEY, account_id INTEGER NOT NULL, "
                "trigger VARCHAR(20) NOT NULL, status VARCHAR(20) NOT NULL, "
                "started_at DATETIME NOT NULL, completed_at DATETIME, steps_json TEXT NOT NULL, "
                "items_fetched INTEGER NOT NULL, quota_units_estimate INTEGER NOT NULL, "
                "error_summary TEXT)"
            )
        )
        for run_id in (1, 2):
            connection.execute(
                text(
                    "INSERT INTO sync_runs (account_id, trigger, status, started_at, steps_json, "
                    "items_fetched, quota_units_estimate) VALUES "
                    f"(1, 'manual', 'running', '2026-10-01 00:0{run_id}:00', '[]', 0, 0)"
                )
            )
    engine.dispose()


def test_0013_adds_lease_columns_and_enforces_one_running_sync_per_account(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "migration-data")
    settings.database_path.parent.mkdir(parents=True, exist_ok=True)
    config = _config(settings)
    _seed_pre_0013_schema(settings.database_url)

    command.upgrade(config, "head")
    engine = create_engine(settings.database_url)
    inspector = inspect(engine)
    for table in JOB_TABLES:
        assert {column["name"] for column in inspector.get_columns(table)} >= LEASE_COLUMNS
        index_names = {index["name"] for index in inspector.get_indexes(table)}
        assert f"ix_{table}_lease_expires_at" in index_names
    sync_index_names = {index["name"] for index in inspector.get_indexes("sync_runs")}
    assert "ux_sync_runs_account_running" in sync_index_names
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, status, error_summary FROM sync_runs ORDER BY id")
        ).all()
        assert [(row.id, row.status) for row in rows] == [(1, "failed"), (2, "running")]
        assert rows[0].error_summary == "interrupted during ownership migration"
        with pytest.raises(IntegrityError):
            connection.execute(
                text(
                    "INSERT INTO sync_runs (account_id, trigger, status, started_at, steps_json, "
                    "items_fetched, quota_units_estimate) VALUES "
                    "(1, 'manual', 'running', '2026-10-01 00:03:00', '[]', 0, 0)"
                )
            )
    engine.dispose()

    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sync_runs (account_id, trigger, status, started_at, steps_json, "
                "items_fetched, quota_units_estimate) VALUES "
                "(1, 'manual', 'completed', '2026-10-01 00:04:00', '[]', 0, 0)"
            )
        )
    engine.dispose()

    command.downgrade(config, "0012_creator_platforms")
    engine = create_engine(settings.database_url)
    inspector = inspect(engine)
    for table in JOB_TABLES:
        assert LEASE_COLUMNS.isdisjoint(column["name"] for column in inspector.get_columns(table))
    assert "ux_sync_runs_account_running" not in {
        index["name"] for index in inspector.get_indexes("sync_runs")
    }
    engine.dispose()
