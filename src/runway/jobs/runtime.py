"""Wires the worker's handlers and producers from settings."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from runway.config import Settings
from runway.db.base import Database
from runway.jobs.queue import JobQueue
from runway.jobs.worker import Handler, JobProducer, Worker
from runway.platforms.sync import (
    SYNC_ACCOUNT_JOB,
    PlatformSyncScheduler,
    SyncService,
    run_sync_account_job,
)


def worker_lock_path(settings: Settings) -> Path:
    """Held for the worker's lifetime: SQLite installs run exactly one worker."""
    return settings.resolved_data_dir / "runtime" / "worker.lock"


def build_worker(settings: Settings, database: Database) -> Worker:
    queue = JobQueue(database)
    sync_service = SyncService(database, settings)
    handlers: dict[str, Handler] = {
        SYNC_ACCOUNT_JOB: partial(run_sync_account_job, sync_service),
    }
    producers: list[JobProducer] = []
    if settings.platform_sync_interval_minutes > 0:
        producers.append(
            PlatformSyncScheduler(database, queue, settings.platform_sync_interval_minutes)
        )
    return Worker(queue=queue, handlers=handlers, producers=producers)
