"""Files under `data/runtime/` that tell operators and the launcher what is running."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from runway.config import Settings


def runtime_directory(settings: Settings) -> Path:
    return settings.resolved_data_dir / "runtime"


def worker_lock_path(settings: Settings) -> Path:
    """Held for the worker's lifetime: SQLite installs run exactly one worker."""
    return runtime_directory(settings) / "worker.lock"


def worker_pid_path(settings: Settings) -> Path:
    """`{"pid", "revision", "started_at"}` of the running worker; removed on exit."""
    return runtime_directory(settings) / "worker.pid"


def write_pid_file(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(".pid.partial")
    partial.write_text(json.dumps(record), encoding="utf-8")
    os.replace(partial, path)


def read_pid_file(path: Path) -> dict[str, Any] | None:
    """The record at `path`, or None when it is missing or unreadable."""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def remove_pid_file(path: Path, pid: int) -> None:
    """Remove `path` only if it still names `pid` (a newer worker may own it)."""
    record = read_pid_file(path)
    if record is not None and record.get("pid") == pid:
        path.unlink(missing_ok=True)
