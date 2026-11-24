"""RT3 item 6: the standby seam sits at the top of every guarded handler.

A machine that does not hold `lease.json` runs no sync, refresh,
subscription or purge jobs (spec 07 section 4). These tests prove
`runway.realtime.standby.ensure_active` asks the private-instance lease and
is invoked before each handler does anything else.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from runway.config import Settings
from runway.jobs.retention import run_purge_job
from runway.platforms.sync import run_sync_account_job
from runway.realtime import standby
from runway.realtime.processing import run_process_webhook_job
from runway.realtime.subscriptions import (
    run_renew_subscription_job,
    run_subscribe_apps_job,
)
from runway.realtime.token_upkeep import run_refresh_token_job


def test_ensure_active_never_raises_without_a_private_instance(tmp_path: Path) -> None:
    """A plain local install is its only writer."""
    assert standby.ensure_active(Settings(data_dir=tmp_path / "data")) is None


def test_ensure_active_raises_on_a_standby_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import runway.operations.instance_lease as lease

    monkeypatch.setattr(lease, "instance_is_active", lambda _settings: False)
    with pytest.raises(standby.StandbyError):
        standby.ensure_active(Settings(data_dir=tmp_path / "data"))


def test_every_guarded_handler_hits_the_seam_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class StandbySentinel(Exception):
        pass

    seen: list[Settings] = []

    def boom(settings: Settings) -> None:
        seen.append(settings)
        raise StandbySentinel

    monkeypatch.setattr(standby, "ensure_active", boom)

    settings = Settings(data_dir=tmp_path / "data")
    job = MagicMock()
    # The deps never get used: the seam raises before the handler body.
    service = MagicMock(settings=settings)
    registry = MagicMock(settings=settings)
    database = MagicMock()
    queue = MagicMock()

    handlers = [
        lambda: run_sync_account_job(service, job),
        lambda: run_process_webhook_job(database, settings, queue, job),
        lambda: run_renew_subscription_job(database, settings, queue, job),
        lambda: run_subscribe_apps_job(registry, job),
        lambda: run_refresh_token_job(registry, job),
        lambda: run_purge_job(database, settings, job),
    ]
    for call in handlers:
        with pytest.raises(StandbySentinel):
            call()

    assert len(seen) == len(handlers)
    assert all(entry is settings for entry in seen)
