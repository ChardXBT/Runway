"""`runway worker` as a real process: signals, single instance, migration check."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from runway.config import Settings
from runway.db import initialize_database

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
        [sys.executable, "-m", "runway.cli.main", "worker"],
        cwd=cwd,
        env=_env(data_dir),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        creationflags=flags,
    )


def _lines(process: subprocess.Popen[str]) -> queue.Queue[str]:
    lines: queue.Queue[str] = queue.Queue()

    def pump() -> None:
        assert process.stdout is not None
        for line in process.stdout:
            lines.put(line)

    threading.Thread(target=pump, daemon=True).start()
    return lines


def _wait_for(lines: queue.Queue[str], text: str, timeout: float = 90) -> str:
    deadline = time.monotonic() + timeout
    seen: list[str] = []
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=0.5)
        except queue.Empty:
            continue
        seen.append(line)
        if text in line:
            return line
    raise AssertionError(f"{text!r} not seen in worker output:\n{''.join(seen)}")


def _stop(process: subprocess.Popen[str]) -> None:
    if sys.platform == "win32":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        process.send_signal(signal.SIGTERM)


@pytest.fixture()
def migrated(tmp_path: Path) -> Path:
    data_dir = tmp_path / "data"
    initialize_database(Settings(data_dir=data_dir)).engine.dispose()
    return data_dir


def test_worker_stops_cleanly_on_a_signal_and_is_single_instance(
    migrated: Path, tmp_path: Path
) -> None:
    worker = _start(migrated, tmp_path)
    try:
        lines = _lines(worker)
        _wait_for(lines, "runway worker start")

        second = subprocess.run(
            [sys.executable, "-m", "runway.cli.main", "worker"],
            cwd=tmp_path,
            env=_env(migrated),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert second.returncode == 3
        assert "already running" in second.stderr

        _stop(worker)
        assert worker.wait(timeout=60) == 0
        _wait_for(lines, "runway worker stop", timeout=10)
    finally:
        if worker.poll() is None:
            worker.kill()
            worker.wait(timeout=30)

    # The OS released the single-instance lock with the process.
    again = subprocess.run(
        [sys.executable, "-m", "runway.cli.main", "worker", "--once"],
        cwd=tmp_path,
        env=_env(migrated),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert again.returncode == 0, again.stdout + again.stderr


def test_worker_refuses_a_database_behind_head(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "runway.cli.main", "worker", "--once"],
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
