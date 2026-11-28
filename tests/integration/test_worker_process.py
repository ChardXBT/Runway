"""`runway worker` as a real process: signals, single instance, migration check."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path

import pytest

from runway.config import Settings
from runway.db import Database, initialize_database
from runway.db.models import PublishJob, utcnow
from runway.jobs.queue import JobQueue
from runway.locks import LockTimeout, exclusive_file_lock
from runway.operations.runtime_files import read_pid_file, worker_lock_path, worker_pid_path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env(data_dir: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("RUNWAY_")}
    env.pop("OPENAI_API_KEY", None)
    env["RUNWAY_DATA_DIR"] = str(data_dir)
    env["RUNWAY_AGENT_RUNTIME"] = "mock"
    env["RUNWAY_PLATFORM_PROVIDER"] = "mock"
    env["RUNWAY_PLATFORM_SYNC_INTERVAL_MINUTES"] = "60"
    return env


def _start(data_dir: Path, cwd: Path) -> subprocess.Popen[str]:
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == "win32" else 0
    return subprocess.Popen(
        # The launcher's entry point; it starts faster than the full CLI.
        [sys.executable, "-m", "runway.cli.worker_commands"],
        cwd=cwd,
        env=_env(data_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=flags,
    )


def _output(process: subprocess.Popen[str]) -> list[str]:
    lines: list[str] = []

    def pump() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.append(line)

    threading.Thread(target=pump, daemon=True).start()
    return lines


def _stop(process: subprocess.Popen[str]) -> None:
    if sys.platform == "win32":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        process.send_signal(signal.SIGTERM)


def _lock_is_free(settings: Settings) -> bool:
    try:
        with exclusive_file_lock(worker_lock_path(settings), timeout=0):
            return True
    except LockTimeout:
        return False


@pytest.fixture()
def migrated(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    initialize_database(Settings(data_dir=data_dir)).engine.dispose()
    return data_dir


def test_worker_stops_cleanly_on_a_signal_and_releases_its_lock_and_pid_record(
    migrated: Path, tmp_path: Path
) -> None:
    # One worker process; readiness is its PID record (written while it holds
    # the lock), not a log line. Single-instance refusal is covered in-process
    # by tests/unit/test_worker_startup.py.
    settings = Settings(data_dir=migrated)
    pid_path = worker_pid_path(settings)
    worker = _start(migrated, tmp_path)
    output = _output(worker)
    try:
        deadline = time.monotonic() + 120
        while read_pid_file(pid_path) is None and worker.poll() is None:
            assert time.monotonic() < deadline, "".join(output)
            time.sleep(0.1)
        record = read_pid_file(pid_path)
        assert record is not None, "".join(output)
        # On Windows the venv python.exe is a redirector; the record names the
        # real interpreter, its child.
        assert isinstance(record["pid"], int) and record["pid"] > 0
        assert not _lock_is_free(settings)
        # Let it reach its idle wait (up to 30 s with a 60-minute interval).
        deadline = time.monotonic() + 60
        while not any("runway worker start" in line for line in output):
            assert time.monotonic() < deadline and worker.poll() is None, "".join(output)
            time.sleep(0.1)
        time.sleep(0.5)

        stopped_at = time.monotonic()
        _stop(worker)
        assert worker.wait(timeout=60) == 0, "".join(output)
        # A stop request does not wait out the idle sleep.
        assert time.monotonic() - stopped_at < 10, "".join(output)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=30)
    deadline = time.monotonic() + 5
    while not any("runway worker stop" in line for line in output):
        assert time.monotonic() < deadline, "".join(output)
        time.sleep(0.05)
    assert not pid_path.exists()
    assert _lock_is_free(settings)


def test_worker_once_surfaces_an_unknown_kind_without_touching_it_and_repeats_after_restart(
    migrated: Path, tmp_path: Path
) -> None:
    settings = Settings(data_dir=migrated, platform_provider="mock")
    database = Database(settings)
    try:
        unknown = JobQueue(database).enqueue(
            "future_kind",
            idempotency_key="operator-surface-fixture",
            run_at=utcnow() - timedelta(minutes=1),
        )
    finally:
        database.engine.dispose()

    outputs: list[str] = []
    for _restart in range(2):
        result = subprocess.run(
            [sys.executable, "-m", "runway.cli.worker_commands", "--once"],
            cwd=tmp_path,
            env=_env(migrated),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        outputs.append(result.stdout + result.stderr)

    for output in outputs:
        warnings = [
            line
            for line in output.splitlines()
            if "does not handle pending durable job kind" in line
        ]
        assert len(warnings) == 1
        assert "future_kind" in warnings[0]
        assert "rolling-deploy compatibility" in warnings[0]

    database = Database(settings)
    try:
        with database.session() as session:
            row = session.get(PublishJob, unknown.id)
            assert row is not None
            assert (row.state, row.attempts, row.locked_by, row.locked_until) == (
                "pending",
                0,
                None,
                None,
            )
    finally:
        database.engine.dispose()


def test_worker_refuses_a_database_behind_head(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "runway.cli.worker_commands", "--once"],
        cwd=tmp_path,
        env=_env(tmp_path / "empty"),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 1
    assert "runway db upgrade" in result.stderr
    assert not (tmp_path / "empty" / "runway.db").exists()
