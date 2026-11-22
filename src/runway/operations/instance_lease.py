"""The single-active-machine lease for a private Runway instance (spec 07 §4).

``lease.json`` in the instance directory names the machine allowed to refresh
tokens and write to platforms. Every other machine runs in standby. Lease
changes reach other machines through the private repository, whose
fast-forward-only ``git push`` acts as the compare-and-swap.
"""

from __future__ import annotations

import json
import os
import socket
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from runway.config import Settings
from runway.platforms.base import ProviderError

LEASE_FILE_NAME = "lease.json"
LEASE_FORMAT_VERSION = 1
LEASE_STALE_AFTER = timedelta(hours=24)
WORKER_BUSY_MARKER = Path("runtime") / "worker.busy"

WorkerBusyCheck = Callable[[Settings], bool]


class StandbyError(ProviderError):
    """This machine does not hold the instance lease, so it may not refresh or write tokens.

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


def machine_id(settings: Settings) -> str:
    """This machine's lease identity: ``RUNWAY_MACHINE_ID`` or the host name."""
    value = settings.machine_id or socket.gethostname()
    return value.strip().casefold()


def read_lease(instance_dir: Path) -> Lease | None:
    path = instance_dir / LEASE_FILE_NAME
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LeaseError(f"{path} cannot be read: {exc}") from exc
    return Lease.from_json(payload)


def write_lease(instance_dir: Path, lease: Lease) -> None:
    instance_dir.mkdir(parents=True, exist_ok=True)
    text = json.dumps(lease.to_json(), indent=2, sort_keys=True) + "\n"
    fd, temp_name = tempfile.mkstemp(dir=instance_dir, prefix=".lease-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
        os.replace(temp_name, instance_dir / LEASE_FILE_NAME)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def describe_lease(lease: Lease | None) -> str:
    if lease is None or lease.holder is None:
        return "nobody holds the lease"
    since = _iso(lease.holder_since) or "an unknown time"
    heartbeat = _iso(lease.heartbeat_at) or "never"
    return f"{lease.holder} holds the lease since {since} (last heartbeat {heartbeat})"


def acquire(lease: Lease | None, me: str, now: datetime, *, take_stale: bool) -> Lease:
    """Return the lease with ``me`` as holder, or raise if another machine has it."""
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


def instance_is_active(settings: Settings) -> bool:
    """True when this machine may refresh tokens and write to platforms.

    Without ``RUNWAY_INSTANCE_DIR`` there is no lease and the machine is always
    active, exactly as before private instances existed.
    """
    instance_dir = settings.resolved_instance_dir
    if instance_dir is None:
        return True
    try:
        lease = read_lease(instance_dir)
    except LeaseError:
        return False
    return lease is not None and lease.holder == machine_id(settings)


def ensure_active(settings: Settings) -> None:
    if instance_is_active(settings):
        return
    instance_dir = settings.resolved_instance_dir
    try:
        lease = read_lease(instance_dir) if instance_dir is not None else None
        held = describe_lease(lease)
    except LeaseError:
        held = f"{LEASE_FILE_NAME} is unreadable"
    raise StandbyError(
        f"this machine ({machine_id(settings)}) is in standby: {held}. "
        "Token refresh and platform writes are disabled; run 'runway instance pull' "
        "after the active machine hands over."
    )


def worker_is_busy(settings: Settings) -> bool:
    """Whether a publish is running. The worker will own this marker; today it is a file."""
    return (settings.resolved_data_dir / WORKER_BUSY_MARKER).exists()
