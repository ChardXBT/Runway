"""Wires the worker's handlers and producers from settings."""

from __future__ import annotations

from functools import partial
from pathlib import Path

from runway.config import Settings
from runway.db.base import Database
from runway.jobs.queue import JobQueue
from runway.jobs.retention import PURGE_JOB, RetentionPurgeScheduler, run_purge_job
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
    # Publishing (spec 03): send/poll_processing/verify/reconcile handlers, and
    # the expired-send rule (re-run only before the request-sent marker).
    from runway.posts.engine import build_publishing_engine
    from runway.posts.jobs import PROBE_MEDIA_JOB
    from runway.posts.runtime import build_media_service

    publishing = build_publishing_engine(settings, database)
    media = build_media_service(settings, database, registry=publishing.registry)
    queue = JobQueue(database, expired_send_handler=publishing.handle_expired_send)
    sync_service = SyncService(database, settings)
    handlers: dict[str, Handler] = {
        SYNC_ACCOUNT_JOB: partial(run_sync_account_job, sync_service),
        PURGE_JOB: partial(run_purge_job, database),
        **publishing.handlers(),
        PROBE_MEDIA_JOB: media.run_probe_job,
    }
    producers: list[JobProducer] = [RetentionPurgeScheduler(queue)]
    if settings.platform_sync_interval_minutes > 0:
        producers.append(
            PlatformSyncScheduler(database, queue, settings.platform_sync_interval_minutes)
        )
    return Worker(queue=queue, handlers=handlers, producers=producers)
