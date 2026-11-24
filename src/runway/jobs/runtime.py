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
from runway.platforms.token_upkeep import (
    REFRESH_TOKEN_JOB,
    TokenUpkeepScheduler,
    run_refresh_token_job,
)

__all__ = ["build_worker", "worker_lock_path"]


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
        PURGE_JOB: partial(run_purge_job, database, settings),
        REFRESH_TOKEN_JOB: partial(run_refresh_token_job, database, settings),
        **publishing.handlers(),
        PROBE_MEDIA_JOB: media.run_probe_job,
    }
    # Instagram long-lived tokens die at day 60 when nothing syncs; upkeep
    # renews them from a job instead (spec 08 section 3).
    producers: list[JobProducer] = [
        RetentionPurgeScheduler(queue),
        TokenUpkeepScheduler(database, queue, settings),
    ]
    if settings.platform_sync_interval_minutes > 0:
        producers.append(
            PlatformSyncScheduler(database, queue, settings.platform_sync_interval_minutes)
        )
    return Worker(queue=queue, handlers=handlers, producers=producers)
