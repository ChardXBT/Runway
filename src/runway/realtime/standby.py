"""Standby seam for the private-instance lease (spec 07 section 4).

Several machines may run one private instance, but only the `lease.json`
holder is active: a machine that is not the holder runs in read-only
standby for platform writes and token refresh - no sync, refresh,
subscription or purge jobs. Two writers corrupt the single-writer
database and TikTok's rotating refresh tokens.

The lease module (``runway.operations.instance_lease``) merges later
with the private-instance work. When it lands, connect its
``ensure_active_or_raise(settings)`` inside ``ensure_active`` below so a
non-holder's raise defers the job through the queue's retry instead of
running it. The call sites, one at the top of each guarded handler, are:

- ``platforms.sync.run_sync_account_job`` (sync)
- ``realtime.processing.run_process_webhook_job`` (sync-adjacent writes)
- ``realtime.subscriptions.run_renew_subscription_job`` (subscription)
- ``realtime.subscriptions.run_subscribe_apps_job`` (subscription)
- ``realtime.token_upkeep.run_refresh_token_job`` (refresh)
- ``jobs.retention.run_purge_job`` (purge)
"""

from __future__ import annotations

from runway.config import Settings


class StandbyError(Exception):
    """This machine is not the active lease holder; the job must defer."""


def ensure_active(settings: Settings) -> None:
    """No-op today: every Runway install is the only writer it knows.

    The merge that brings ``runway.operations.instance_lease`` connects
    the real check here - one raise and every guarded job defers.
    """
