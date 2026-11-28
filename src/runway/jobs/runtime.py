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
from runway.workspaces.lifecycle import (
    DELETE_WORKSPACE_JOB,
    EXPORT_WORKSPACE_JOB,
    run_delete_workspace_job,
    run_export_workspace_job,
)

__all__ = ["build_worker", "worker_lock_path"]


def build_worker(settings: Settings, database: Database) -> Worker:
    # Publishing (spec 03): send/poll_processing/verify/reconcile handlers, and
    # the expired-send rule (re-run only before the request-sent marker).
    # Outbound webhooks (spec 06 section 8): fan-out producer and delivery handler.
    from runway.agent.webhooks import (
        DELIVER_WEBHOOK_JOB,
        WebhookDispatcher,
        WebhookService,
        run_deliver_webhook_job,
    )
    from runway.posts.engine import build_publishing_engine
    from runway.posts.jobs import PROBE_MEDIA_JOB
    from runway.posts.runtime import build_media_service

    publishing = build_publishing_engine(settings, database)
    media = build_media_service(settings, database, registry=publishing.registry)
    queue = JobQueue(database, expired_send_handler=publishing.handle_expired_send)
    sync_service = SyncService(database, settings)
    webhooks = WebhookService(database, settings)
    registry = ProviderRegistry(database, settings)
    handlers: dict[str, Handler] = {
        SYNC_ACCOUNT_JOB: partial(run_sync_account_job, sync_service),
        PURGE_JOB: partial(run_purge_job, database, settings),
        **publishing.handlers(),
        PROBE_MEDIA_JOB: media.run_probe_job,
        DELIVER_WEBHOOK_JOB: partial(run_deliver_webhook_job, webhooks),
        PROCESS_WEBHOOK_JOB: partial(run_process_webhook_job, database, settings, queue),
        RENEW_SUBSCRIPTION_JOB: partial(run_renew_subscription_job, database, settings, queue),
        # Instagram push gating: only ever enqueued while the Meta app is
        # Live with Advanced Access (spec 04 section 2).
        SUBSCRIBE_APPS_JOB: partial(run_subscribe_apps_job, registry),
        REFRESH_TOKEN_JOB: partial(run_refresh_token_job, registry),
        # Workspace data duties (spec 05 sections 4-5): the export artifact and
        # the purge after a workspace delete.
        EXPORT_WORKSPACE_JOB: partial(run_export_workspace_job, database, settings),
        DELETE_WORKSPACE_JOB: partial(run_delete_workspace_job, database, settings),
    }
    producers: list[JobProducer] = [
        RetentionPurgeScheduler(queue),
        WebhookDispatcher(webhooks, queue),
        # WebSub leases are renewed at 80% and resubscribed after expiry.
        SubscriptionScheduler(database, queue),
        # Instagram long-lived tokens die at day 60 when nothing syncs;
        # upkeep enqueues their day-50 refresh job instead (spec 04
        # section 2, spec 08 section 3).
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
    from runway.operations.instance_lease import instance_is_active, worker_busy

    return Worker(
        queue=queue,
        handlers=handlers,
        producers=producers,
        is_active=partial(instance_is_active, settings),
        busy=partial(worker_busy, settings),
    )
