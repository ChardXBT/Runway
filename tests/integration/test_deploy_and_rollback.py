"""Deploy and rollback support: `/health` identity, `db upgrade`/`db downgrade`, launch status."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from alembic import command
from runway.api.app import create_app
from runway.cli import db_commands
from runway.cli.main import app as cli_app
from runway.config import Settings
from runway.db import initialize_database
from runway.db.backup import backup_directory
from runway.db.bootstrap import alembic_config, migration_head, migration_status
from runway.locks import exclusive_file_lock
from runway.operations.launch_status import launch_status
from runway.operations.revision import checkout_revision
from runway.operations.runtime_files import worker_lock_path, worker_pid_path

REPO_ROOT = Path(__file__).resolve().parents[2]
OLDEST = db_commands.OLDEST_SUPPORTED_DOWNGRADE


def _printed_json(stdout: str) -> dict[str, object]:
    start = stdout.rfind("\n{\n")
    payload: object = json.loads(stdout[start + 1 :] if start >= 0 else stdout)
    assert isinstance(payload, dict)
    return payload


def _version(settings: Settings) -> str | None:
    with sqlite3.connect(settings.database_path) as connection:
        row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
    connection.close()
    return None if row is None else str(row[0])


@pytest.fixture()
def migrated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    settings = Settings(data_dir=tmp_path / "data")
    initialize_database(settings).engine.dispose()
    monkeypatch.setattr(db_commands, "get_settings", lambda: settings)
    return settings


def test_health_adds_the_code_identity_without_changing_existing_fields(
    migrated: Settings,
) -> None:
    with TestClient(create_app(migrated), base_url="http://127.0.0.1:8000") as client:
        payload = client.get("/health").json()

    # What the Supervisor and the launcher already check stays as it was.
    assert payload["product"] == "Runway"
    assert payload["status"] == "ok"
    for existing in ("publishing_enabled", "one_bot_post_per_day", "platform_provider"):
        assert existing in payload
    # New: lets the launcher tell an older API from this checkout.
    assert payload["revision"] == checkout_revision()
    assert payload["migration_head"] == migration_head(migrated)
    assert payload["sync_scheduler"] == "worker"


def test_downgrade_refuses_targets_below_0012_without_force(migrated: Settings) -> None:
    head = migration_head(migrated)
    result = CliRunner().invoke(cli_app, ["db", "downgrade", "0011_runtime_performance"])

    assert result.exit_code == 2, result.output
    assert OLDEST in result.output
    assert _version(migrated) == head
    assert not backup_directory(migrated).exists()


def test_downgrade_to_0012_backs_up_first_then_rolls_back(migrated: Settings) -> None:
    head = migration_head(migrated)

    result = CliRunner().invoke(cli_app, ["db", "downgrade", OLDEST])

    assert result.exit_code == 0, result.output
    payload = _printed_json(result.stdout)
    assert payload["previous"] == head
    assert payload["current"] == OLDEST
    assert _version(migrated) == OLDEST
    backup = Path(str(payload["backup"]))
    assert backup.parent == backup_directory(migrated)
    # The backup is the database as it was before the downgrade.
    with sqlite3.connect(backup) as copy:
        assert copy.execute("SELECT version_num FROM alembic_version").fetchone() == (head,)
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    copy.close()


def test_downgrade_below_0012_with_force_is_allowed(migrated: Settings) -> None:
    result = CliRunner().invoke(cli_app, ["db", "downgrade", "0011_runtime_performance", "--force"])
    assert result.exit_code == 0, result.output
    assert _version(migrated) == "0011_runtime_performance"


def test_downgrade_and_a_pending_upgrade_refuse_while_the_worker_runs(
    migrated: Settings,
) -> None:
    head = migration_head(migrated)
    runner = CliRunner()
    with exclusive_file_lock(worker_lock_path(migrated), timeout=0):
        refused = runner.invoke(cli_app, ["db", "downgrade", OLDEST])
        assert refused.exit_code == db_commands.EXIT_WORKER_RUNNING
        assert "stop `runway worker`" in refused.output
        assert _version(migrated) == head
        # Nothing to migrate: upgrade is a harmless no-op even with a worker up.
        noop = runner.invoke(cli_app, ["db", "upgrade"])
        assert noop.exit_code == 0, noop.output

    command.downgrade(alembic_config(migrated), OLDEST)
    with exclusive_file_lock(worker_lock_path(migrated), timeout=0):
        pending = runner.invoke(cli_app, ["db", "upgrade"])
    assert pending.exit_code == db_commands.EXIT_WORKER_RUNNING
    assert _version(migrated) == OLDEST
    assert not backup_directory(migrated).exists()


def test_a_pending_upgrade_backs_up_first(migrated: Settings) -> None:
    command.downgrade(alembic_config(migrated), OLDEST)

    result = CliRunner().invoke(cli_app, ["db", "upgrade"])

    assert result.exit_code == 0, result.output
    payload = _printed_json(result.stdout)
    assert payload["previous"] == OLDEST
    assert payload["current"] == migration_head(migrated)
    backup = Path(str(payload["backup"]))
    with sqlite3.connect(backup) as copy:
        assert copy.execute("SELECT version_num FROM alembic_version").fetchone() == (OLDEST,)
    copy.close()
    # Nothing pending: no new backup.
    again = _printed_json(CliRunner().invoke(cli_app, ["db", "upgrade"]).stdout)
    assert again["backup"] is None


def test_launch_status_reports_what_the_launcher_compares(migrated: Settings) -> None:
    command.downgrade(alembic_config(migrated), OLDEST)

    status = launch_status(migrated)

    assert status["current"] == OLDEST
    assert status["head"] == migration_head(migrated)
    assert status["up_to_date"] is False
    assert status["revision"] == checkout_revision()
    assert status["worker_lock"] == str(worker_lock_path(migrated))
    assert status["worker_pid"] == str(worker_pid_path(migrated))
    assert migration_status(migrated).current == OLDEST


def test_launch_status_runs_as_a_module_and_prints_only_json(tmp_path: Path) -> None:
    import os

    env = {key: value for key, value in os.environ.items() if not key.startswith("RUNWAY_")}
    env["RUNWAY_DATA_DIR"] = str(tmp_path / "fresh")
    result = subprocess.run(
        [sys.executable, "-m", "runway.operations.launch_status"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["current"] is None
    assert payload["up_to_date"] is False
    # Reading the status never creates the database.
    assert not (tmp_path / "fresh" / "runway.db").exists()


def test_checkout_revision_is_this_repositorys_commit() -> None:
    expected = subprocess.run(
        ["git", "rev-parse", "--short=12", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.strip()
    assert checkout_revision() == (expected or "unknown")
