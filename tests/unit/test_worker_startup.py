"""`runway worker` startup: stale-run sweep guard, PID record and single instance."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError
from typer.testing import CliRunner

from runway.cli import worker_commands
from runway.cli.main import app as cli_app
from runway.config import Settings
from runway.locks import exclusive_file_lock
from runway.operations.runtime_files import (
    read_pid_file,
    remove_pid_file,
    worker_lock_path,
    worker_pid_path,
    write_pid_file,
)


def _locked() -> OperationalError:
    return OperationalError("UPDATE sync_runs", {}, Exception("database is locked"))


class _FlakyService:
    def __init__(self, failures: list[BaseException]) -> None:
        self.failures = failures
        self.calls = 0

    def mark_stale_runs_failed(self) -> int:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return 0


def test_the_stale_run_sweep_retries_a_locked_database() -> None:
    service = _FlakyService([_locked(), _locked()])
    sleeps: list[float] = []

    worker_commands.sweep_stale_runs(service, sleep=sleeps.append)  # type: ignore[arg-type]

    assert service.calls == 3
    assert sleeps == [0.5, 1.0]


def test_the_stale_run_sweep_never_crashes_the_worker() -> None:
    locked_forever = _FlakyService([_locked() for _ in range(10)])
    worker_commands.sweep_stale_runs(locked_forever, attempts=3, sleep=lambda _s: None)  # type: ignore[arg-type]
    assert locked_forever.calls == 3

    broken = _FlakyService([RuntimeError("no such table: sync_runs")])
    worker_commands.sweep_stale_runs(broken, sleep=lambda _s: None)  # type: ignore[arg-type]
    assert broken.calls == 1


class _FakeWorker:
    def __init__(self, pid_path: Path) -> None:
        self.pid_path = pid_path
        self.seen: dict[str, object] | None = None

        class _Event:
            def is_set(self) -> bool:
                return False

        self.stop_event = _Event()

    def stop(self) -> None:
        pass

    def produce(self) -> None:
        self.seen = read_pid_file(self.pid_path)

    def run_once(self) -> bool:
        return False


@pytest.fixture()
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    active = Settings(data_dir=tmp_path / "data")
    monkeypatch.setattr(worker_commands, "get_settings", lambda: active)
    monkeypatch.setattr(worker_commands, "require_database_at_head", lambda _settings: None)
    monkeypatch.setattr(worker_commands, "sweep_stale_runs", lambda _service: None)
    return active


def test_the_worker_records_its_pid_while_running_and_removes_it_on_exit(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_path = worker_pid_path(settings)
    fake = _FakeWorker(pid_path)
    monkeypatch.setattr(worker_commands, "build_worker", lambda _settings, _database: fake)

    result = CliRunner().invoke(cli_app, ["worker", "--once"])

    assert result.exit_code == 0, result.output
    assert fake.seen is not None
    assert fake.seen["pid"] == os.getpid()
    assert isinstance(fake.seen["revision"], str) and fake.seen["revision"]
    assert not pid_path.exists()


def test_a_second_worker_exits_3_and_leaves_the_running_workers_pid_record(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_path = worker_pid_path(settings)
    write_pid_file(pid_path, {"pid": 424242, "revision": "abc", "started_at": "x"})
    monkeypatch.setattr(
        worker_commands,
        "build_worker",
        lambda _settings, _database: pytest.fail("a second worker must not build"),
    )

    with exclusive_file_lock(worker_lock_path(settings), timeout=0):
        result = CliRunner().invoke(cli_app, ["worker", "--once"])

    assert result.exit_code == worker_commands.EXIT_ALREADY_RUNNING
    assert "already running" in result.output
    assert read_pid_file(pid_path) == {"pid": 424242, "revision": "abc", "started_at": "x"}


def test_a_lock_timeout_inside_a_running_worker_is_not_reported_as_already_running(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runway.locks import LockTimeout

    class _Exploding(_FakeWorker):
        def produce(self) -> None:
            raise LockTimeout("timed out waiting for account-1.lock")

    monkeypatch.setattr(
        worker_commands,
        "build_worker",
        lambda _settings, _database: _Exploding(worker_pid_path(settings)),
    )

    result = CliRunner().invoke(cli_app, ["worker", "--once"])

    assert result.exit_code != worker_commands.EXIT_ALREADY_RUNNING
    assert "already running" not in result.output
    assert not worker_pid_path(settings).exists()


def test_pid_records_are_removed_only_by_their_owner(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "worker.pid"
    write_pid_file(path, {"pid": 11})
    remove_pid_file(path, 12)
    assert json.loads(path.read_text(encoding="utf-8")) == {"pid": 11}
    remove_pid_file(path, 11)
    assert not path.exists()
    assert read_pid_file(path) is None
    path.write_text("not json", encoding="utf-8")
    assert read_pid_file(path) is None


def test_jobs_unknown_is_an_operator_visible_rolling_deploy_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runway.cli import job_commands
    from runway.db import initialize_database
    from runway.jobs.queue import JobQueue

    active = Settings(data_dir=tmp_path / "data", platform_provider="mock")
    database = initialize_database(active)
    try:
        queue = JobQueue(database)
        queue.enqueue("purge", idempotency_key="known")
        queue.enqueue("future_kind", idempotency_key="unknown-1")
        queue.enqueue("future_kind", idempotency_key="unknown-2")
        monkeypatch.setattr(job_commands, "get_settings", lambda: active)

        result = CliRunner().invoke(cli_app, ["jobs", "unknown"])

        assert result.exit_code == 0, result.output
        start = result.output.rfind("\n{\n")
        payload = json.loads(result.output[start + 1 :] if start >= 0 else result.output)
        assert payload["policy"] == "pending_for_rolling_deploy_compatibility"
        assert payload["unknown_pending_jobs"] == [{"kind": "future_kind", "pending": 2}]
        assert "purge" not in {job["kind"] for job in payload["unknown_pending_jobs"]}
    finally:
        database.engine.dispose()
