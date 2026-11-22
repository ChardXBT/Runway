"""The single-active-machine lease for a private Runway instance (spec 07 §4).

``lease.json`` in the instance directory names the machine allowed to refresh
tokens, write to platforms and run the worker. Every other machine runs in
standby. Lease changes reach other machines through the private repository,
whose fast-forward-only ``git push`` acts as the compare-and-swap.

A local ``lease.json`` naming this machine is not enough to be active: the
remote copy is re-read when the last confirmation is older than
``REMOTE_CHECK_INTERVAL``, so a machine whose stale lease was taken over drops
to standby within minutes. When the remote cannot be read, the machine stays in
standby (fail closed).
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import socket
import sys
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from runway.config import Settings
from runway.platforms.base import ProviderError

logger = logging.getLogger(__name__)

LEASE_FILE_NAME = "lease.json"
# Matches the instance repository's ``.lease-*.json`` ignore rule: never committed.
LEASE_CHECK_FILE_NAME = ".lease-check.json"
LEASE_FORMAT_VERSION = 1
LEASE_STALE_AFTER = timedelta(hours=24)
REMOTE_CHECK_INTERVAL = timedelta(minutes=10)
# The active machine renews its heartbeat once it is this old, so a running
# instance never goes more than about six hours without a remote heartbeat.
HEARTBEAT_RENEW_AFTER = timedelta(hours=5)
LEGACY_WORKER_BUSY_MARKER = Path("runtime") / "worker.busy"
WORKER_BUSY_DIR = Path("runtime") / "busy"
MACHINE_ID_FILE_ENV = "RUNWAY_MACHINE_ID_FILE"

WorkerBusyCheck = Callable[[Settings], bool]
RemoteLeaseReader = Callable[[Settings], "Lease | None"]


class StandbyError(ProviderError):
    """This machine does not hold the instance lease, so it may not refresh tokens,
    write to platforms or run the worker.

    A ``ProviderError`` so existing sync and inbox paths treat it as a failed,
    non-reauthorization outcome: the account stays connected.
    """

    def __init__(self, detail: str) -> None:
        super().__init__(detail, retryable=False)


class LeaseError(RuntimeError):
    """The lease cannot be taken or changed by this machine."""


@dataclass(frozen=True)
class Lease:
    holder: str | None
    holder_since: datetime | None
    heartbeat_at: datetime | None
    released_at: datetime | None = None
    released_by: str | None = None

    def is_held(self) -> bool:
        return self.holder is not None

    def is_stale(self, now: datetime) -> bool:
        if self.holder is None:
            return False
        if self.heartbeat_at is None:
            return True
        return now - self.heartbeat_at > LEASE_STALE_AFTER

    def to_json(self) -> dict[str, Any]:
        return {
            "format_version": LEASE_FORMAT_VERSION,
            "holder": self.holder,
            "holder_since": _iso(self.holder_since),
            "heartbeat_at": _iso(self.heartbeat_at),
            "released_at": _iso(self.released_at),
            "released_by": self.released_by,
        }

    @classmethod
    def from_json(cls, payload: object) -> Lease:
        if not isinstance(payload, dict) or payload.get("format_version") != LEASE_FORMAT_VERSION:
            raise LeaseError(f"{LEASE_FILE_NAME} has an unsupported format")
        holder = payload.get("holder")
        released_by = payload.get("released_by")
        return cls(
            holder=str(holder) if holder else None,
            holder_since=_parse(payload.get("holder_since")),
            heartbeat_at=_parse(payload.get("heartbeat_at")),
            released_at=_parse(payload.get("released_at")),
            released_by=str(released_by) if released_by else None,
        )

    @classmethod
    def from_text(cls, text: str) -> Lease:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LeaseError(f"{LEASE_FILE_NAME} is not valid JSON") from exc
        return cls.from_json(payload)


def _iso(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _parse(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        raise LeaseError(f"{LEASE_FILE_NAME} has an invalid timestamp") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


# --- machine identity ----------------------------------------------------------------------


def machine_id_file() -> Path:
    """Where this user's machine id lives: the user profile, outside every repository."""
    explicit = os.environ.get(MACHINE_ID_FILE_ENV)
    if explicit:
        return Path(explicit)
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / "Runway" / "machine-id"
    state = os.environ.get("XDG_STATE_HOME")
    base = Path(state) if state else Path.home() / ".local" / "state"
    return base / "runway" / "machine-id"


def _new_machine_id() -> str:
    host = re.sub(r"[^a-z0-9-]+", "-", socket.gethostname().casefold()).strip("-") or "machine"
    # A random suffix: two machines with the same host name never share an identity.
    return f"{host[:40]}-{secrets.token_hex(6)}"


def local_machine_id() -> str:
    """The id stored in the user profile, created once on first use."""
    path = machine_id_file()
    if not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass  # another process created it first; read theirs
        else:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(_new_machine_id() + "\n")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise LeaseError(f"{path} is empty; delete it and Runway creates a new machine id")
    return value


def machine_id(settings: Settings) -> str:
    """This machine's lease identity: ``RUNWAY_MACHINE_ID`` or the id in the user profile."""
    value = settings.machine_id or local_machine_id()
    return value.strip().casefold()


# --- lease file -----------------------------------------------------------------------------


def read_lease(instance_dir: Path) -> Lease | None:
    path = instance_dir / LEASE_FILE_NAME
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LeaseError(f"{path} cannot be read: {exc}") from exc
    return Lease.from_text(text)


def _atomic_write(directory: Path, name: str, text: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=directory, prefix=".lease-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp_name, directory / name)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def write_lease(instance_dir: Path, lease: Lease) -> None:
    _atomic_write(
        instance_dir, LEASE_FILE_NAME, json.dumps(lease.to_json(), indent=2, sort_keys=True) + "\n"
    )


def describe_lease(lease: Lease | None) -> str:
    if lease is None or lease.holder is None:
        return "nobody holds the lease"
    since = _iso(lease.holder_since) or "an unknown time"
    heartbeat = _iso(lease.heartbeat_at) or "never"
    return f"{lease.holder} holds the lease since {since} (last heartbeat {heartbeat})"


def acquire(lease: Lease | None, me: str, now: datetime, *, take_stale: bool) -> Lease:
    """Return the lease with ``me`` as holder, or raise if another machine has it.

    Callers pass the lease as read from the remote repository, so staleness is
    judged by the remote heartbeat, never by an out-of-date local copy.
    """
    if lease is None or lease.holder is None:
        return Lease(holder=me, holder_since=now, heartbeat_at=now)
    if lease.holder == me:
        return replace(lease, heartbeat_at=now)
    if not lease.is_stale(now):
        raise LeaseError(
            f"{describe_lease(lease)}. Run 'runway instance handover' on {lease.holder} first."
        )
    if not take_stale:
        raise LeaseError(
            f"{describe_lease(lease)}, which is older than 24 hours. "
            "If that machine is gone, pass --take to take the lease."
        )
    return Lease(holder=me, holder_since=now, heartbeat_at=now)


def release(lease: Lease | None, me: str, now: datetime) -> Lease:
    if lease is None or lease.holder != me:
        raise LeaseError(f"this machine ({me}) does not hold the lease: {describe_lease(lease)}")
    return Lease(holder=None, holder_since=None, heartbeat_at=None, released_at=now, released_by=me)


# --- remote confirmation ---------------------------------------------------------------------


def _default_remote_reader(settings: Settings) -> Lease | None:
    from runway.operations.instance import read_remote_lease

    return read_remote_lease(settings)


# Tests replace this; production reads lease.json from the private repository.
remote_lease_reader: RemoteLeaseReader = _default_remote_reader


def record_remote_check(instance_dir: Path, holder: str | None, checked_at: datetime) -> None:
    payload = {"checked_at": checked_at.astimezone(UTC).isoformat(), "holder": holder}
    _atomic_write(instance_dir, LEASE_CHECK_FILE_NAME, json.dumps(payload) + "\n")


def _recent_remote_holder(instance_dir: Path, now: datetime) -> tuple[bool, str | None]:
    """(fresh, holder) from the last remote confirmation."""
    path = instance_dir / LEASE_CHECK_FILE_NAME
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        checked_at = datetime.fromisoformat(str(payload["checked_at"]))
    except (OSError, ValueError, KeyError, TypeError):
        return False, None
    age = now - checked_at
    fresh = timedelta(0) <= age < REMOTE_CHECK_INTERVAL
    holder = payload.get("holder")
    return fresh, str(holder) if holder else None


def instance_is_active(settings: Settings) -> bool:
    """True when this machine may refresh tokens, write to platforms and run the worker.

    Without ``RUNWAY_INSTANCE_DIR`` there is no lease and the machine is always
    active, exactly as before private instances existed.
    """
    instance_dir = settings.resolved_instance_dir
    if instance_dir is None:
        return True
    try:
        local = read_lease(instance_dir)
        me = machine_id(settings)
    except LeaseError:
        return False
    if local is None or local.holder != me:
        return False
    now = datetime.now(UTC)
    fresh, holder = _recent_remote_holder(instance_dir, now)
    if fresh:
        return holder == me
    try:
        remote = remote_lease_reader(settings)
    except (RuntimeError, OSError) as exc:
        logger.warning("instance lease could not be confirmed remotely; standby: %s", exc)
        return False
    remote_holder = remote.holder if remote is not None else None
    record_remote_check(instance_dir, remote_holder, now)
    return remote_holder == me


def ensure_active_or_raise(settings: Settings) -> None:
    """The one gate for token refresh, token writes, platform writes and worker jobs."""
    if instance_is_active(settings):
        return
    instance_dir = settings.resolved_instance_dir
    lease: Lease | None = None
    try:
        lease = read_lease(instance_dir) if instance_dir is not None else None
        held = describe_lease(lease)
    except LeaseError:
        held = f"{LEASE_FILE_NAME} is unreadable"
    if instance_dir is not None and lease is not None and lease.holder == machine_id(settings):
        fresh, remote_holder = _recent_remote_holder(instance_dir, datetime.now(UTC))
        held = (
            f"the private repository says {remote_holder or 'nobody'} holds the lease"
            if fresh
            else "the lease could not be confirmed with the private repository"
        )
    raise StandbyError(
        f"this machine ({machine_id(settings)}) is in standby: {held}. "
        "Token refresh, platform writes and the worker are disabled; run "
        "'runway instance pull' after the active machine hands over."
    )


ensure_active = ensure_active_or_raise


# --- worker busy markers -------------------------------------------------------------------


def worker_busy_markers(settings: Settings) -> list[Path]:
    data_dir = settings.resolved_data_dir
    markers = sorted((data_dir / WORKER_BUSY_DIR).glob("*.busy"))
    legacy = data_dir / LEGACY_WORKER_BUSY_MARKER
    if legacy.exists():
        markers.insert(0, legacy)
    return markers


def worker_is_busy(settings: Settings) -> bool:
    """Whether a publish or other platform write is running on this machine."""
    return bool(worker_busy_markers(settings))


def mark_worker_busy(settings: Settings, job: str) -> Path:
    """Record that a job which must not overlap a snapshot has started; returns its marker."""
    directory = settings.resolved_data_dir / WORKER_BUSY_DIR
    directory.mkdir(parents=True, exist_ok=True)
    marker = directory / f"{os.getpid()}-{secrets.token_hex(4)}.busy"
    payload = {"job": job, "pid": os.getpid(), "started_at": datetime.now(UTC).isoformat()}
    marker.write_text(json.dumps(payload) + "\n", encoding="utf-8")
    return marker


def clear_worker_busy(marker: Path) -> None:
    marker.unlink(missing_ok=True)


@contextmanager
def worker_busy(settings: Settings, job: str) -> Iterator[None]:
    marker = mark_worker_busy(settings, job)
    try:
        yield
    finally:
        clear_worker_busy(marker)
