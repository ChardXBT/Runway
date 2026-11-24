"""Wires the worker's handlers and producers from settings."""

from __future__ import annotations

from functools import partial

from runway.config import Settings
from runway.db.base import Database
from runway.jobs.queue import JobQueue
from runway.jobs.retention import PURGE_JOB, RetentionPurgeScheduler, run_purge_job
from runway.jobs.worker import Handler, JobProducer, Worker
from runway.operations.runtime_files import worker_lock_path
from runway.platforms.sync import (
    SYNC_ACCOUNT_JOB,
    PlatformSyncScheduler,
    SyncService,
    run_sync_account_job,
)
from runway.realtime.processing import PROCESS_WEBHOOK_JOB, run_process_webhook_job
from runway.realtime.schedule import AdaptiveSyncScheduler

__all__ = ["build_worker", "worker_lock_path"]


def build_worker(settings: Settings, database: Database) -> Worker:
    queue = JobQueue(database)
    sync_service = SyncService(database, settings)
    handlers: dict[str, Handler] = {
        SYNC_ACCOUNT_JOB: partial(run_sync_account_job, sync_service),
        PURGE_JOB: partial(run_purge_job, database, settings),
        PROCESS_WEBHOOK_JOB: partial(run_process_webhook_job, database, settings, queue),
    }
    producers: list[JobProducer] = [RetentionPurgeScheduler(queue)]
    if settings.adaptive_sync_enabled:
        # Per-scope cadence (spec 04 section 3); replaces the fixed-slot
        # scheduler. platform_sync_interval_minutes only governs the latter.
        producers.append(AdaptiveSyncScheduler(database, queue, settings))
    elif settings.platform_sync_interval_minutes > 0:
        producers.append(
            PlatformSyncScheduler(database, queue, settings.platform_sync_interval_minutes)
        )
    return Worker(queue=queue, handlers=handlers, producers=producers)
