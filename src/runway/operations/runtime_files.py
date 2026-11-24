"""Paths of the small runtime marker files under the data directory.

The ``runtime`` directory also holds the worker busy markers
(``runway.operations.instance_lease``) and the instance transfer marker.
"""

from __future__ import annotations

from pathlib import Path

from runway.config import Settings


def worker_lock_path(settings: Settings) -> Path:
    """Held by `runway worker` for its lifetime (one worker per data directory)."""
    return settings.resolved_data_dir / "runtime" / "worker.lock"


def api_lock_path(settings: Settings) -> Path:
    """Held by the API process for its lifetime.

    ``runway instance pull`` probes it (and the worker's lock) so it never
    replaces the database underneath a running Runway. It is separate from the
    worker's lock so the API never stops a worker from starting.
    """
    return settings.resolved_data_dir / "runtime" / "api.lock"
