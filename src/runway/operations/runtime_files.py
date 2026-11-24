"""Paths of the small runtime marker files under the data directory.

The ``runtime`` directory also holds the worker busy markers
(``runway.operations.instance_lease``) and the instance transfer marker.
"""

from __future__ import annotations

from pathlib import Path

from runway.config import Settings


def worker_lock_path(settings: Settings) -> Path:
    """The lock a running Runway (the API process and its workers) holds.

    The API keeps this locked for its whole lifetime so ``runway instance
    pull`` can tell that this machine's Runway is still running: replacing the
    database underneath it would corrupt its state (and on Windows fails
    outright). See ``runway.locks.exclusive_file_lock``.
    """
    return settings.resolved_data_dir / "runtime" / "worker.lock"
