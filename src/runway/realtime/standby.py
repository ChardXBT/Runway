"""Standby seam for the private-instance lease (spec 07 section 4).

Several machines may run one private instance, but only the `lease.json`
holder is active: a machine that is not the holder runs in read-only
standby for platform writes and token refresh - no sync, refresh,
subscription or purge jobs. Two writers corrupt the single-writer
database and TikTok's rotating refresh tokens.

``ensure_active`` below asks ``runway.operations.instance_lease`` (the
cached lease check, no network). The call sites, one at the top of each
guarded handler, are:

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
    """Raise `StandbyError` unless this machine holds the private-instance lease.

    Without ``RUNWAY_INSTANCE_DIR`` every install is its only writer and this
    never raises. The worker already claims no job on a standby machine; this
    is the second line for handlers reached another way.
    """
    from runway.operations.instance_lease import instance_is_active

    if not instance_is_active(settings):
        raise StandbyError("this machine is on standby for the private instance")
