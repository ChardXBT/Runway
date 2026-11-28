"""The API never migrates; `runway db upgrade` is the only migration entry point."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
from runway.db.models import ConnectedAccount

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


def test_the_api_schedules_and_runs_no_sync_even_with_an_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scheduled sync is produced and executed by `runway worker` only (audit C8).

    Inspects the real mechanisms instead of a thread name: building a sync
    scheduler (the old in-process one took a SyncService, the new producer a
    queue), building a worker, starting any sync, or a background thread running
    platform or job code would all be recorded; and no job may be queued.
    """
    import sqlite3
    import threading

    from runway.jobs import runtime, worker
    from runway.platforms import sync

    settings = Settings(data_dir=tmp_path / "data", platform_sync_interval_minutes=1)
    database = initialize_database(settings)
    with database.session() as session:
        session.add(
            ConnectedAccount(
                platform="youtube",
                external_account_id="channel-1",
                status="connected",
                granted_scopes_json="[]",
            )
        )
    database.engine.dispose()

    calls: list[str] = []

    def record(name: str) -> Callable[..., None]:
        def recorder(*_args: object, **_kwargs: object) -> None:
            calls.append(name)

        return recorder

    real_scheduler_init = sync.PlatformSyncScheduler.__init__

    def scheduler_init(self: sync.PlatformSyncScheduler, *args: Any, **kwargs: Any) -> None:
        calls.append("PlatformSyncScheduler")
        real_scheduler_init(self, *args, **kwargs)

    monkeypatch.setattr(sync.PlatformSyncScheduler, "__init__", scheduler_init)
    monkeypatch.setattr(sync.PlatformSyncScheduler, "produce", record("produce"))
    monkeypatch.setattr(sync.SyncService, "run", record("SyncService.run"))
    monkeypatch.setattr(sync.SyncService, "start_background", record("start_background"))
    monkeypatch.setattr(runtime, "build_worker", record("build_worker"))
    monkeypatch.setattr(worker.Worker, "run_forever", record("Worker.run_forever"))

    started: list[str] = []
    real_start = threading.Thread.start

    def thread_start(self: threading.Thread) -> None:
        target = getattr(self, "_target", None)
        started.append(f"{getattr(target, '__module__', '')}.{getattr(target, '__qualname__', '')}")
        real_start(self)

    monkeypatch.setattr(threading.Thread, "start", thread_start)

    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        health = client.get("/health").json()

    assert calls == []
    assert not [name for name in started if name.startswith(("runway.platforms", "runway.jobs"))]
    assert health["sync_scheduler"] == "worker"
    with sqlite3.connect(settings.database_path) as connection:
        queued = connection.execute("SELECT COUNT(*) FROM publish_jobs").fetchone()[0]
        runs = connection.execute("SELECT COUNT(*) FROM sync_runs").fetchone()[0]
    connection.close()
    assert (queued, runs) == (0, 0)


@pytest.mark.parametrize(
    "directory_name",
    ["run#way", "per%20cent %41"] + ([] if sys.platform == "win32" else ["what?mode=rw"]),
)
def test_a_data_directory_with_uri_characters_reads_as_migrated(
    tmp_path: Path, directory_name: str
) -> None:
    settings = Settings(data_dir=tmp_path / directory_name / "data")
    initialize_database(settings).engine.dispose()

    status = migration_status(settings)

    assert status.current == status.head
    require_database_at_head(settings)
    # Reading never created a stray database next to the real one.
    assert sorted(path.name for path in (tmp_path / directory_name).iterdir()) == ["data"]
