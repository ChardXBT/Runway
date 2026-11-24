"""Wires the worker's handlers and producers from settings."""

from __future__ import annotations

from functools import partial

from runway.config import Settings
from runway.db.base import Database
from runway.jobs.queue import JobQueue
from runway.jobs.retention import PURGE_JOB, RetentionPurgeScheduler, run_purge_job
from runway.jobs.worker import Handler, JobProducer, Worker
from runway.operations.runtime_files import worker_lock_path
from runway.platforms.registry import ProviderRegistry
from runway.platforms.sync import (
    SYNC_ACCOUNT_JOB,
    PlatformSyncScheduler,
    SyncService,
    run_sync_account_job,
)
from runway.realtime.processing import PROCESS_WEBHOOK_JOB, run_process_webhook_job
from runway.realtime.schedule import AdaptiveSyncScheduler
from runway.realtime.subscriptions import (
    RENEW_SUBSCRIPTION_JOB,
    SUBSCRIBE_APPS_JOB,
    SubscriptionScheduler,
    run_renew_subscription_job,
    run_subscribe_apps_job,
)
from runway.realtime.token_upkeep import (
    REFRESH_TOKEN_JOB,
    TokenUpkeepScheduler,
    run_refresh_token_job,
)

__all__ = ["build_worker", "worker_lock_path"]


def build_worker(settings: Settings, database: Database) -> Worker:
    queue = JobQueue(database)
    sync_service = SyncService(database, settings)
    registry = ProviderRegistry(database, settings)
    handlers: dict[str, Handler] = {
        SYNC_ACCOUNT_JOB: partial(run_sync_account_job, sync_service),
        PURGE_JOB: partial(run_purge_job, database, settings),
        PROCESS_WEBHOOK_JOB: partial(run_process_webhook_job, database, settings, queue),
        RENEW_SUBSCRIPTION_JOB: partial(run_renew_subscription_job, database, settings, queue),
        # Instagram push gating: only ever enqueued while the Meta app is
        # Live with Advanced Access (spec 04 section 2).
        SUBSCRIBE_APPS_JOB: partial(run_subscribe_apps_job, registry),
        REFRESH_TOKEN_JOB: partial(run_refresh_token_job, registry),
    }
    producers: list[JobProducer] = [
        RetentionPurgeScheduler(queue),
        # WebSub leases are renewed at 80% and resubscribed after expiry.
        SubscriptionScheduler(database, queue),
        # Instagram's 60-day tokens get their day-50 refresh job.
        TokenUpkeepScheduler(database, registry, queue),
    ]
    if settings.adaptive_sync_enabled:
        # Per-scope cadence (spec 04 section 3); replaces the fixed-slot
        # scheduler. platform_sync_interval_minutes only governs the latter.
        producers.append(AdaptiveSyncScheduler(database, queue, settings))
    elif settings.platform_sync_interval_minutes > 0:
        producers.append(
            PlatformSyncScheduler(database, queue, settings.platform_sync_interval_minutes)
        )
    return Worker(queue=queue, handlers=handlers, producers=producers)
