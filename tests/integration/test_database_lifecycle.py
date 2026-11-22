"""The API never migrates; `runway db upgrade` is the only migration entry point."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from alembic import command
from runway.api.app import create_app
from runway.cli import db_commands
from runway.cli.main import app as cli_app
from runway.config import Settings
from runway.db import initialize_database
from runway.db.bootstrap import (
    DatabaseNotCurrent,
    alembic_config,
    migration_status,
    require_database_at_head,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _subprocess_env(data_dir: Path) -> dict[str, str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("RUNWAY_") and key not in {"OPENAI_API_KEY"}
    }
    env["RUNWAY_DATA_DIR"] = str(data_dir)
    env["RUNWAY_AGENT_RUNTIME"] = "mock"
    env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT), str(REPO_ROOT / "src")])
    return env


def test_create_app_neither_migrates_nor_creates_the_database(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")

    app = create_app(settings)

    assert app.state.database is not None
    assert not settings.database_path.exists()


def test_importing_the_launcher_entry_points_creates_no_database(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    # The working directory has no .env, so only RUNWAY_DATA_DIR is configured.
    code = "import runway.api.app; import runpy; runpy.run_path(r'{}')".format(
        REPO_ROOT / "apps" / "api" / "main.py"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        env=_subprocess_env(data_dir),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not (data_dir / "runway.db").exists()


def test_api_refuses_to_start_on_an_unmigrated_database(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")

    with (
        pytest.raises(DatabaseNotCurrent, match="runway db upgrade"),
        TestClient(create_app(settings), base_url="http://127.0.0.1:8000"),
    ):
        pass

    assert not settings.database_path.exists()


def test_api_refuses_a_database_behind_head_and_leaves_it_untouched(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    settings.ensure_directories()
    command.upgrade(alembic_config(settings), "0011_runtime_performance")

    with pytest.raises(DatabaseNotCurrent, match="behind head"):
        require_database_at_head(settings)
    with (
        pytest.raises(DatabaseNotCurrent),
        TestClient(create_app(settings), base_url="http://127.0.0.1:8000"),
    ):
        pass

    assert migration_status(settings).current == "0011_runtime_performance"


def test_api_starts_once_the_database_is_at_head(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    initialize_database(settings).engine.dispose()

    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        assert client.get("/health").status_code == 200


def test_a_revision_unknown_to_this_code_is_refused(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    initialize_database(settings).engine.dispose()
    import sqlite3

    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("UPDATE alembic_version SET version_num = '9999_from_the_future'")
    connection.close()

    status = migration_status(settings)
    assert status.known is False
    with pytest.raises(DatabaseNotCurrent, match="does not know"):
        require_database_at_head(settings)


def _printed_json(stdout: str) -> dict[str, object]:
    """The command's indented JSON report; JSON log lines may precede it."""
    start = stdout.rfind("\n{\n")
    payload: object = json.loads(stdout[start + 1 :] if start >= 0 else stdout)
    assert isinstance(payload, dict)
    return payload


def test_db_current_and_upgrade_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    monkeypatch.setattr(db_commands, "get_settings", lambda: settings)
    runner = CliRunner()

    before = runner.invoke(cli_app, ["db", "current"])
    assert before.exit_code == 0, before.output
    payload = _printed_json(before.stdout)
    assert payload["current"] is None
    assert payload["up_to_date"] is False
    # Reading the revision must not create the database file.
    assert not settings.database_path.exists()

    upgraded = runner.invoke(cli_app, ["db", "upgrade"])
    assert upgraded.exit_code == 0, upgraded.output
    upgrade_payload = _printed_json(upgraded.stdout)
    assert upgrade_payload["previous"] is None
    assert upgrade_payload["current"] == upgrade_payload["head"]

    after = _printed_json(runner.invoke(cli_app, ["db", "current"]).stdout)
    assert after["up_to_date"] is True
    assert after["current"] == upgrade_payload["head"]

    again = runner.invoke(cli_app, ["db", "upgrade"])
    assert again.exit_code == 0
    assert _printed_json(again.stdout)["previous"] == upgrade_payload["head"]


def test_alembic_config_names_the_configured_channel(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data", channel_name="Northwind")
    config: Config = alembic_config(settings)
    assert config.attributes["runway_channel_name"] == "Northwind"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def test_uvicorn_exits_non_zero_with_a_clear_error_when_not_migrated(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "runway.api.app:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(_free_port()),
        ],
        cwd=tmp_path,
        env=_subprocess_env(data_dir),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode != 0
    assert "runway db upgrade" in result.stderr + result.stdout
    assert not (data_dir / "runway.db").exists()
