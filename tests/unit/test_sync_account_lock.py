"""A manual sync (API) and a scheduled sync (worker) of one account never overlap."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ConnectedAccount, SyncRun, utcnow
from runway.locks import exclusive_file_lock
from runway.platforms.sync import SyncRunInProgress, SyncService, sync_lock_path


@pytest.fixture()
def mock_settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", platform_provider="mock")


@pytest.fixture()
def mock_database(mock_settings: Settings):  # type: ignore[no-untyped-def]
    from runway.db import initialize_database

    database = initialize_database(mock_settings)
    yield database
    database.engine.dispose()


def _account(database: Database) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform="youtube",
            external_account_id="UC-lock",
            status="connected",
            granted_scopes_json=json.dumps([]),
        )
        session.add(account)
        session.flush()
        return account.id


def _runs(database: Database) -> list[SyncRun]:
    with database.session() as session:
        return list(session.scalars(select(SyncRun)).all())


def test_a_sync_held_by_another_service_instance_is_refused(
    mock_settings: Settings, mock_database: Database
) -> None:
    """Two SyncService instances have separate thread locks, like two processes."""
    account_id = _account(mock_database)
    with exclusive_file_lock(sync_lock_path(mock_settings, account_id), timeout=0):
        with pytest.raises(SyncRunInProgress, match="another Runway process"):
            SyncService(mock_database, mock_settings).run(account_id)
        with pytest.raises(SyncRunInProgress):
            SyncService(mock_database, mock_settings).start_background(account_id)
    # Refused before any run row was written.
    assert _runs(mock_database) == []

    # Once the lock is free the sync goes ahead (it fails here: no stored token).
    run = SyncService(mock_database, mock_settings).run(account_id)
    assert run.status != "running"
    assert len(_runs(mock_database)) == 1


HOLD = r"""
import sys, time
from pathlib import Path
from runway.locks import exclusive_file_lock
with exclusive_file_lock(Path(sys.argv[1]), timeout=0):
    Path(sys.argv[2]).write_text("held")
    time.sleep(30)
"""


def test_a_sync_in_another_process_blocks_this_one(
    mock_settings: Settings, mock_database: Database, tmp_path: Path
) -> None:
    account_id = _account(mock_database)
    flag = tmp_path / "held.flag"
    holder = subprocess.Popen(
        [sys.executable, "-c", HOLD, str(sync_lock_path(mock_settings, account_id)), str(flag)]
    )
    try:
        deadline = time.monotonic() + 60
        while not flag.exists():
            assert holder.poll() is None and time.monotonic() < deadline
            time.sleep(0.05)
        with pytest.raises(SyncRunInProgress):
            SyncService(mock_database, mock_settings).run(account_id)
    finally:
        holder.kill()
        holder.wait(timeout=30)
    assert _runs(mock_database) == []


def test_the_stale_run_sweep_leaves_a_run_whose_sync_is_still_live(
    mock_settings: Settings, mock_database: Database
) -> None:
    account_id = _account(mock_database)
    with mock_database.session() as session:
        session.add(
            SyncRun(
                account_id=account_id,
                trigger="scheduled",
                status="running",
                started_at=utcnow() - timedelta(hours=2),
            )
        )
    service = SyncService(mock_database, mock_settings)
    with exclusive_file_lock(sync_lock_path(mock_settings, account_id), timeout=0):
        assert service.mark_stale_runs_failed() == 0
    assert service.mark_stale_runs_failed() == 1
    assert [run.status for run in _runs(mock_database)] == ["failed"]
