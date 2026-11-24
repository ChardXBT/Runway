from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest
from cryptography.fernet import Fernet

from runway.config import Settings
from runway.db import Database, initialize_database
from runway.operations import instance_lease
from runway.operations.instance_lease import (
    Lease,
    LeaseError,
    StandbyError,
    acquire,
    ensure_active,
    instance_is_active,
    read_lease,
    release,
    worker_is_busy,
    write_lease,
)
from runway.platforms.base import ProviderError, TokenSet
from runway.platforms.capabilities import Platform
from runway.platforms.registry import ProviderRegistry
from runway.platforms.tokens import TokenStore, TokenStoreError

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


class RemoteLease:
    """Stands in for the private repository's copy of lease.json."""

    def __init__(self) -> None:
        self.lease: Lease | None = None
        self.mirror_local = True
        self.reads = 0
        self.error: Exception | None = None

    def __call__(self, settings: Settings) -> Lease | None:
        self.reads += 1
        if self.error is not None:
            raise self.error
        if self.mirror_local:
            assert settings.resolved_instance_dir is not None
            return read_lease(settings.resolved_instance_dir)
        return self.lease


@pytest.fixture(autouse=True)
def remote(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RemoteLease:
    monkeypatch.setenv("RUNWAY_MACHINE_ID_FILE", str(tmp_path / "profile" / "machine-id"))
    fake = RemoteLease()
    monkeypatch.setattr(instance_lease, "remote_lease_reader", fake)
    return fake


def _settings(tmp_path: Path, machine: str, *, key: str | None = None) -> Settings:
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir(exist_ok=True)
    return Settings(
        data_dir=tmp_path / "data",
        instance_dir=instance_dir,
        machine_id=machine,
        token_key=key,
    )


def test_acquire_free_own_fresh_and_stale_leases() -> None:
    taken = acquire(None, "machine-a", NOW, take_stale=False)
    assert taken == Lease(holder="machine-a", holder_since=NOW, heartbeat_at=NOW)

    later = NOW + timedelta(hours=3)
    renewed = acquire(taken, "machine-a", later, take_stale=False)
    assert renewed.holder_since == NOW
    assert renewed.heartbeat_at == later

    with pytest.raises(LeaseError, match="handover"):
        acquire(taken, "machine-b", NOW + timedelta(hours=23), take_stale=True)

    stale_time = NOW + timedelta(hours=24, seconds=1)
    assert taken.is_stale(stale_time)
    with pytest.raises(LeaseError, match="--take"):
        acquire(taken, "machine-b", stale_time, take_stale=False)
    assert acquire(taken, "machine-b", stale_time, take_stale=True).holder == "machine-b"


def test_release_requires_the_holder_and_frees_the_lease() -> None:
    held = Lease(holder="machine-a", holder_since=NOW, heartbeat_at=NOW)
    with pytest.raises(LeaseError, match="does not hold"):
        release(held, "machine-b", NOW)
    released = release(held, "machine-a", NOW)
    assert released.holder is None
    assert released.released_by == "machine-a"
    assert acquire(released, "machine-b", NOW, take_stale=False).holder == "machine-b"


def test_lease_round_trips_and_rejects_unknown_formats(tmp_path: Path) -> None:
    lease = Lease(holder="machine-a", holder_since=NOW, heartbeat_at=NOW)
    write_lease(tmp_path, lease)
    assert read_lease(tmp_path) == lease
    assert json.loads((tmp_path / "lease.json").read_text())["holder"] == "machine-a"

    (tmp_path / "lease.json").write_text('{"format_version": 99}', encoding="utf-8")
    with pytest.raises(LeaseError, match="unsupported"):
        read_lease(tmp_path)


def test_without_an_instance_every_machine_is_active(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    assert instance_is_active(settings)
    ensure_active(settings)


def _lease_and_check(
    settings: Settings, holder: str, *, checked_at: datetime | None = None
) -> None:
    """The local lease plus a fresh remote confirmation naming ``holder``."""
    assert settings.resolved_instance_dir is not None
    write_lease(
        settings.resolved_instance_dir,
        Lease(holder=holder, holder_since=NOW, heartbeat_at=NOW),
    )
    instance_lease.record_remote_check(
        settings.resolved_instance_dir, holder, checked_at or datetime.now(UTC)
    )


def test_only_the_lease_holder_is_active(tmp_path: Path) -> None:
    holder = _settings(tmp_path, "machine-a")
    other = _settings(tmp_path, "machine-b")
    assert holder.resolved_instance_dir is not None
    # No lease yet: nobody is active.
    assert not instance_is_active(holder)

    _lease_and_check(holder, "machine-a")
    assert instance_is_active(holder)
    assert not instance_is_active(other)
    with pytest.raises(StandbyError, match="machine-a holds the lease") as caught:
        ensure_active(other)
    # A ProviderError that is not AuthorizationExpired: sync keeps the account connected.
    assert isinstance(caught.value, ProviderError)

    (holder.resolved_instance_dir / "lease.json").write_text("{broken", encoding="utf-8")
    assert not instance_is_active(holder)


def test_instance_token_store_uses_the_secret_key_and_never_writes_a_key_file(
    tmp_path: Path,
) -> None:
    key = Fernet.generate_key().decode("ascii")
    settings = _settings(tmp_path, "machine-a", key=key)
    assert settings.resolved_instance_dir is not None
    _lease_and_check(settings, "machine-a")
    store = TokenStore.from_settings(settings)
    store.store("ref", TokenSet(access_token="access-value"))

    assert store.store_path == settings.resolved_instance_dir / "tokens" / "platform-tokens.json"
    loaded = store.load("ref")
    assert loaded is not None and loaded.access_token == "access-value"
    assert not list(tmp_path.rglob("*.key"))

    without_key = TokenStore.from_settings(_settings(tmp_path, "machine-a"))
    with pytest.raises(TokenStoreError, match="RUNWAY_TOKEN_KEY"):
        without_key.load("ref")


class _CountingProvider:
    def __init__(self) -> None:
        self.refreshes = 0

    def refresh(self, token: TokenSet) -> TokenSet:
        self.refreshes += 1
        return token.model_copy(update={"expires_at": datetime.now(UTC) + timedelta(hours=1)})


def test_a_machine_without_the_lease_cannot_refresh_or_write_tokens(tmp_path: Path) -> None:
    key = Fernet.generate_key().decode("ascii")
    active = _settings(tmp_path, "machine-a", key=key)
    standby = _settings(tmp_path, "machine-b", key=key)
    assert active.resolved_instance_dir is not None
    _lease_and_check(active, "machine-a")
    expiring = TokenSet(
        access_token="old-access",
        refresh_token="rotating-refresh",
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )
    TokenStore.from_settings(active).store("ref", expiring)

    standby_registry = ProviderRegistry(initialize_database(standby), standby)
    standby_provider = _CountingProvider()
    standby_registry.set_provider(Platform.tiktok, standby_provider)  # type: ignore[arg-type]
    with pytest.raises(StandbyError):
        standby_registry.fresh_token(Platform.tiktok, "ref")
    # The refresh request is never sent: TikTok would rotate the active machine's token.
    assert standby_provider.refreshes == 0
    with pytest.raises(StandbyError):
        standby_registry.token_store.store("ref", expiring)
    with pytest.raises(StandbyError):
        standby_registry.token_store.delete("ref")
    # Reading stays possible in standby.
    loaded = standby_registry.token_store.load("ref")
    assert loaded is not None and loaded.access_token == "old-access"

    active_registry = ProviderRegistry(initialize_database(active), active)
    active_provider = _CountingProvider()
    active_registry.set_provider(Platform.tiktok, active_provider)  # type: ignore[arg-type]
    refreshed = active_registry.fresh_token(Platform.tiktok, "ref")
    assert active_provider.refreshes == 1
    assert refreshed.expires_at is not None and refreshed.expires_at > expiring.expires_at  # type: ignore[operator]


def test_worker_busy_marker(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    assert not worker_is_busy(settings)
    marker = settings.resolved_data_dir / "runtime" / "worker.busy"
    marker.parent.mkdir(parents=True)
    marker.write_text("publishing", encoding="utf-8")
    assert worker_is_busy(settings)


# --- review fixes -------------------------------------------------------------------------


def test_machine_id_defaults_to_an_id_created_once_in_the_user_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RUNWAY_MACHINE_ID", raising=False)
    settings = Settings(data_dir=tmp_path / "data")
    first = instance_lease.machine_id(settings)
    assert first == instance_lease.machine_id(settings)
    stored = tmp_path / "profile" / "machine-id"
    assert stored.read_text(encoding="utf-8").strip() == first
    # Not the host name alone: two machines with the same name get different ids.
    host = instance_lease.socket.gethostname().casefold()
    assert first != host
    monkeypatch.setenv("RUNWAY_MACHINE_ID_FILE", str(tmp_path / "other-profile" / "machine-id"))
    assert instance_lease.machine_id(settings) != first

    assert instance_lease.machine_id(Settings(machine_id="Explicit-Box")) == "explicit-box"


def test_a_holder_whose_lease_was_taken_remotely_drops_to_standby(
    tmp_path: Path, remote: RemoteLease
) -> None:
    settings = _settings(tmp_path, "machine-a")
    assert settings.resolved_instance_dir is not None
    instance_dir = settings.resolved_instance_dir
    write_lease(instance_dir, Lease(holder="machine-a", holder_since=NOW, heartbeat_at=NOW))

    # The last remote confirmation still names this machine: active.
    instance_lease.record_remote_check(instance_dir, "machine-a", datetime.now(UTC))
    assert instance_is_active(settings)

    # The next confirmation the heartbeat or a command records says another
    # machine holds the lease: standby from the cache alone, no remote read.
    instance_lease.record_remote_check(instance_dir, "machine-b", datetime.now(UTC))
    assert not instance_is_active(settings)
    with pytest.raises(StandbyError, match="private repository says machine-b"):
        ensure_active(settings)
    assert remote.reads == 0

    # Back to machine A remotely: the fresh confirmation activates it again.
    instance_lease.record_remote_check(instance_dir, "machine-a", datetime.now(UTC))
    assert instance_is_active(settings)
    assert remote.reads == 0


def test_a_missing_remote_check_means_standby(tmp_path: Path, remote: RemoteLease) -> None:
    settings = _settings(tmp_path, "machine-a")
    assert settings.resolved_instance_dir is not None
    write_lease(
        settings.resolved_instance_dir,
        Lease(holder="machine-a", holder_since=NOW, heartbeat_at=NOW),
    )

    # Nothing confirmed yet: fail closed instead of asking the remote.
    assert not instance_is_active(settings)
    with pytest.raises(StandbyError, match="could not be confirmed"):
        ensure_active(settings)
    assert remote.reads == 0

    instance_lease.record_remote_check(
        settings.resolved_instance_dir, "machine-a", datetime.now(UTC)
    )
    assert instance_is_active(settings)
    assert remote.reads == 0


def test_a_gated_check_never_waits_on_the_remote(
    tmp_path: Path, remote: RemoteLease, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, "machine-a")
    _lease_and_check(settings, "machine-a")
    calls: list[Settings] = []

    def sleepy_reader(value: Settings) -> Lease | None:
        calls.append(value)
        time.sleep(5)  # a hung network fetch must never block a request path
        return None

    monkeypatch.setattr(instance_lease, "remote_lease_reader", sleepy_reader)

    started = time.monotonic()
    assert instance_is_active(settings)
    assert time.monotonic() - started < 0.5
    assert calls == []


def test_a_stale_remote_check_means_standby_and_logs_once(
    tmp_path: Path, remote: RemoteLease, caplog: pytest.LogCaptureFixture
) -> None:
    settings = _settings(tmp_path, "machine-a")
    assert settings.resolved_instance_dir is not None
    instance_dir = settings.resolved_instance_dir
    write_lease(instance_dir, Lease(holder="machine-a", holder_since=NOW, heartbeat_at=NOW))
    aged = datetime.now(UTC) - instance_lease.LEASE_CHECK_MAX_AGE - timedelta(seconds=1)
    instance_lease.record_remote_check(instance_dir, "machine-a", aged)

    import logging

    with caplog.at_level(logging.WARNING, instance_lease.__name__):
        assert not instance_is_active(settings)
        assert not instance_is_active(settings)  # the warning fires once
    warnings = [r for r in caplog.records if "remote confirmation" in r.message]
    assert len(warnings) == 1
    assert remote.reads == 0

    # A fresh confirmation clears the warning state and activates again.
    instance_lease.record_remote_check(instance_dir, "machine-a", datetime.now(UTC))
    assert instance_is_active(settings)


def test_worker_busy_markers_mark_and_clear(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    assert not worker_is_busy(settings)
    marker = instance_lease.mark_worker_busy(settings, "youtube_publish")
    assert worker_is_busy(settings)
    assert json.loads(marker.read_text(encoding="utf-8"))["job"] == "youtube_publish"
    instance_lease.clear_worker_busy(marker)
    assert not worker_is_busy(settings)

    with instance_lease.worker_busy(settings, "comment_reply"):
        assert worker_is_busy(settings)
    assert not worker_is_busy(settings)
    with pytest.raises(RuntimeError), instance_lease.worker_busy(settings, "crash"):
        raise RuntimeError("job failed")
    assert not worker_is_busy(settings)


def test_unexpired_tokens_for_platform_writes_are_refused_in_standby(tmp_path: Path) -> None:
    key = Fernet.generate_key().decode("ascii")
    active = _settings(tmp_path, "machine-a", key=key)
    standby = _settings(tmp_path, "machine-b", key=key)
    assert active.resolved_instance_dir is not None
    _lease_and_check(active, "machine-a")
    TokenStore.from_settings(active).store("ref", TokenSet(access_token="long-lived"))

    standby_registry = ProviderRegistry(cast(Database, None), standby)
    # Reads (sync) still work in standby; writes do not.
    assert standby_registry.fresh_token(Platform.youtube, "ref").access_token == "long-lived"
    with pytest.raises(StandbyError):
        standby_registry.fresh_token(Platform.youtube, "ref", for_write=True)
    with pytest.raises(StandbyError):
        standby_registry.ensure_platform_write_allowed()

    active_registry = ProviderRegistry(cast(Database, None), active)
    token = active_registry.fresh_token(Platform.youtube, "ref", for_write=True)
    assert token.access_token == "long-lived"


def test_local_machine_id_retries_a_transiently_empty_file(tmp_path: Path) -> None:
    path = instance_lease.machine_id_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")  # a create that died between open and write

    def fill_in_the_id() -> None:
        time.sleep(0.06)
        path.write_text("machine-late\n", encoding="utf-8")

    writer = threading.Thread(target=fill_in_the_id)
    writer.start()
    try:
        assert instance_lease.local_machine_id() == "machine-late"
    finally:
        writer.join(timeout=10)


def test_local_machine_id_refuses_a_file_that_stays_empty(tmp_path: Path) -> None:
    path = instance_lease.machine_id_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")

    with pytest.raises(LeaseError, match="empty"):
        instance_lease.local_machine_id()


def test_local_machine_id_never_overwrites_an_existing_id(tmp_path: Path) -> None:
    path = instance_lease.machine_id_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("machine-first\n", encoding="utf-8")

    assert instance_lease.local_machine_id() == "machine-first"
    assert path.read_text(encoding="utf-8") == "machine-first\n"


def test_local_machine_id_falls_back_when_hard_links_are_unsupported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FAT32 and some network shares cannot hard-link: O_EXCL creates instead."""
    path = instance_lease.machine_id_file()

    def no_hard_links(source: str, destination: str) -> None:
        raise OSError("hard links are not supported on this filesystem")

    monkeypatch.setattr(instance_lease.os, "link", no_hard_links)

    created = instance_lease.local_machine_id()
    assert created
    assert path.read_text(encoding="utf-8").strip() == created
    # Second call reads the file the fallback created: same id, no retry.
    assert instance_lease.local_machine_id() == created


def test_a_transfer_marker_makes_the_machine_standby(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "machine-a")
    _lease_and_check(settings, "machine-a")
    assert instance_is_active(settings)

    with instance_lease.instance_transfer(settings, "push"):
        assert not instance_is_active(settings)
        with pytest.raises(StandbyError, match="transfer in progress"):
            ensure_active(settings)
        # Nested (handover calls push): the inner scope must not clear it.
        with instance_lease.instance_transfer(settings, "handover"):
            assert not instance_is_active(settings)
        assert not instance_is_active(settings)

    assert instance_is_active(settings)


def _transfer_marker_with(settings: Settings, **fields: object) -> Path:
    """A marker file written by hand, as a crashed command would leave it."""
    marker = instance_lease.instance_transfer_marker(settings)
    marker.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "operation": "push",
        "pid": os.getpid(),
        "started_at": datetime.now(UTC).isoformat(),
    }
    payload.update(fields)
    marker.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return marker


def test_a_transfer_marker_naming_a_dead_pid_is_ignored_and_deleted(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "machine-a")
    _lease_and_check(settings, "machine-a")
    assert instance_is_active(settings)

    # A finished child leaves a pid nobody owns any more.
    process = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_pid = process.pid
    process.wait(timeout=30)
    marker = _transfer_marker_with(settings, pid=dead_pid)

    assert not instance_lease.instance_transfer_in_progress(settings)
    assert instance_is_active(settings)
    assert not marker.exists()


def test_a_transfer_marker_with_a_live_pid_stays_a_transfer(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "machine-a")
    _lease_and_check(settings, "machine-a")
    marker = _transfer_marker_with(settings, pid=os.getpid())

    assert instance_lease.instance_transfer_in_progress(settings)
    assert not instance_is_active(settings)
    assert marker.is_file()


def test_a_transfer_marker_older_than_six_hours_is_ignored_and_deleted(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, "machine-a")
    _lease_and_check(settings, "machine-a")
    started = datetime.now(UTC) - instance_lease.TRANSFER_MARKER_MAX_AGE - timedelta(seconds=1)
    # Even a live pid cannot hold a machine hostage past the marker's max age.
    marker = _transfer_marker_with(settings, pid=os.getpid(), started_at=started.isoformat())

    assert not instance_lease.instance_transfer_in_progress(settings)
    assert instance_is_active(settings)
    assert not marker.exists()


def test_an_unreadable_transfer_marker_is_judged_by_its_file_age(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "machine-a")
    _lease_and_check(settings, "machine-a")
    marker = instance_lease.instance_transfer_marker(settings)
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_bytes(b"{partial-write-from-a-crash")
    assert instance_lease.instance_transfer_in_progress(settings)  # fresh debris: standby

    ancient = (datetime.now(UTC) - instance_lease.TRANSFER_MARKER_MAX_AGE).timestamp() - 1
    os.utime(marker, (ancient, ancient))
    assert not instance_lease.instance_transfer_in_progress(settings)
    assert not marker.exists()


def test_clear_stale_instance_transfer_only_drops_stale_markers(tmp_path: Path) -> None:
    settings = _settings(tmp_path, "machine-a")
    assert not instance_lease.clear_stale_instance_transfer(settings)

    live = _transfer_marker_with(settings, pid=os.getpid())
    assert not instance_lease.clear_stale_instance_transfer(settings)
    assert live.is_file()

    started = datetime.now(UTC) - instance_lease.TRANSFER_MARKER_MAX_AGE - timedelta(seconds=1)
    live.write_text(json.dumps({"pid": os.getpid(), "started_at": started.isoformat()}))
    assert instance_lease.clear_stale_instance_transfer(settings)
    assert not live.exists()
