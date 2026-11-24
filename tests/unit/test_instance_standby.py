"""Standby blocks the browser publisher and its queue worker (spec 07 §9)."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from runway.config import Settings
from runway.db import Database
from runway.operations import instance_lease
from runway.operations.instance_lease import (
    Lease,
    StandbyError,
    ensure_active_or_raise,
    worker_is_busy,
    write_lease,
)
from runway.publishing.queue import PublisherQueueCoordinator
from runway.publishing.youtube import YouTubeBrowserPublisher

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_remote_reads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Request paths never fetch: any remote read here fails the test."""
    monkeypatch.setenv("RUNWAY_MACHINE_ID_FILE", str(tmp_path / "profile" / "machine-id"))

    def forbidden(settings: Settings) -> Lease:
        raise AssertionError("instance_is_active must never fetch on a request path")

    monkeypatch.setattr(instance_lease, "remote_lease_reader", forbidden)


def _settings(tmp_path: Path, holder: str, **overrides: Any) -> Settings:
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir(parents=True, exist_ok=True)
    write_lease(instance_dir, Lease(holder=holder, holder_since=NOW, heartbeat_at=NOW))
    # The remote confirmed this lease just now, as a heartbeat would record it.
    instance_lease.record_remote_check(instance_dir, holder, datetime.now(UTC))
    return Settings(
        data_dir=tmp_path / "data",
        instance_dir=instance_dir,
        machine_id="this-machine",
        **overrides,
    )


def test_ensure_active_or_raise_is_the_worker_gate(tmp_path: Path) -> None:
    ensure_active_or_raise(Settings(data_dir=tmp_path / "local"))  # no instance: always active
    ensure_active_or_raise(_settings(tmp_path / "a", "this-machine"))
    with pytest.raises(StandbyError, match="worker"):
        ensure_active_or_raise(_settings(tmp_path / "b", "other-machine"))


def test_standby_cannot_confirm_a_youtube_schedule(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        "other-machine",
        publishing_mode="authorized_browser",
        publishing_enabled=True,
        youtube_automation_authorized=True,
    )
    publisher = YouTubeBrowserPublisher(cast(Database, None), settings)
    with pytest.raises(StandbyError):
        asyncio.run(
            publisher.confirm_schedule(1, confirmation_token="token", confirmation_phrase="phrase")
        )
    with pytest.raises(StandbyError):
        asyncio.run(publisher.process_queued_attempt(1))


class _StubPublisher:
    publisher_name = "stub"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.queue = [1]
        self.processed: list[int] = []
        self.busy_while_processing: list[bool] = []
        self.done = threading.Event()

    def next_queued_attempt_id(self) -> int | None:
        return self.queue[0] if self.queue else None

    async def process_queued_attempt(self, attempt_id: int) -> None:
        self.busy_while_processing.append(worker_is_busy(self.settings))
        self.processed.append(attempt_id)
        self.queue.remove(attempt_id)
        self.done.set()


class _StubQueue(PublisherQueueCoordinator):
    def _persisted_queue_state(self) -> tuple[int, str | None, list[int]]:
        return len(cast(_StubPublisher, self.publisher).queue), None, []


def test_the_queue_worker_does_not_start_in_standby(tmp_path: Path) -> None:
    publisher = _StubPublisher(_settings(tmp_path, "other-machine"))
    queue = _StubQueue(cast(YouTubeBrowserPublisher, publisher))

    queue.start()

    assert queue.status()["running"] is False
    assert publisher.processed == []


def test_the_queue_worker_does_not_start_during_an_instance_transfer(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, "this-machine")
    publisher = _StubPublisher(settings)
    queue = _StubQueue(cast(YouTubeBrowserPublisher, publisher))

    with instance_lease.instance_transfer(settings, "push"):
        queue.start()
        assert queue.status()["running"] is False
        assert publisher.processed == []

    queue.start()
    assert publisher.done.wait(10)
    assert publisher.processed == [1]


def test_the_active_queue_worker_marks_itself_busy(tmp_path: Path) -> None:
    publisher = _StubPublisher(_settings(tmp_path, "this-machine"))
    queue = _StubQueue(cast(YouTubeBrowserPublisher, publisher))

    queue.start()

    assert publisher.done.wait(10)
    assert publisher.processed == [1]
    assert publisher.busy_while_processing == [True]
    deadline = datetime.now(UTC).timestamp() + 10
    while queue.status()["running"] and datetime.now(UTC).timestamp() < deadline:
        threading.Event().wait(0.05)
    assert not worker_is_busy(publisher.settings)


def test_the_worker_marks_busy_before_checking_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transfer checking worker_is_busy must never see a gap between the
    worker's active check and its busy marker."""
    settings = _settings(tmp_path, "this-machine")
    publisher = _StubPublisher(settings)
    queue = _StubQueue(cast(YouTubeBrowserPublisher, publisher))
    busy_at_check: list[bool] = []
    real = instance_lease.instance_is_active

    def spy(value: Settings) -> bool:
        # Only the worker thread's loop check counts; _start_if_ready asks first.
        if threading.current_thread().name == "runway-youtube-publisher":
            busy_at_check.append(worker_is_busy(value))
        return real(value)

    monkeypatch.setattr("runway.publishing.queue.instance_is_active", spy)

    queue.start()

    assert publisher.done.wait(10)
    assert publisher.processed == [1]
    assert busy_at_check == [True]
