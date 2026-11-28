from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from runway.config import Settings
from runway.db.base import Database
from runway.db.job_leases import (
    complete_stale_run,
    finish_owned_run,
    lease_claim_fields,
    new_owner_token,
    renew_run_lease,
    run_lease_expired,
)
from runway.db.models import SearchRun
from runway.db.repositories import get_channel


def _add_search_run(
    database: Database,
    settings: Settings,
    *,
    owner_token: str | None = None,
    started_at: datetime | None = None,
    heartbeat_at: datetime | None = None,
    lease_expires_at: datetime | None = None,
    lease_version: int = 0,
) -> int:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
        run = SearchRun(
            channel_id=channel_id,
            style_profile_id=None,
            query_plan_json="{}",
            provider="fixture",
            status="running",
            started_at=started_at or datetime.now(UTC),
            owner_token=owner_token,
            heartbeat_at=heartbeat_at,
            lease_expires_at=lease_expires_at,
            lease_version=lease_version,
        )
        session.add(run)
        session.flush()
        return run.id


def test_lease_renewal_extends_heartbeat_expiry_and_version(
    database: Database, settings: Settings
) -> None:
    owner = new_owner_token()
    started = datetime.now(UTC) - timedelta(minutes=10)
    fields = lease_claim_fields(owner, lease_duration=timedelta(minutes=5), now=started)
    run_id = _add_search_run(
        database,
        settings,
        owner_token=owner,
        started_at=started,
        heartbeat_at=fields["heartbeat_at"],
        lease_expires_at=fields["lease_expires_at"],
        lease_version=fields["lease_version"],
    )
    renewed_at = started + timedelta(minutes=2)

    with database.session() as session:
        assert renew_run_lease(
            session,
            SearchRun,
            run_id,
            owner,
            lease_duration=timedelta(minutes=5),
            now=renewed_at,
        )

    with database.session() as session:
        run = session.get(SearchRun, run_id)
        assert run is not None
        assert run.lease_version == 2
        assert run.heartbeat_at == renewed_at.replace(tzinfo=None)
        assert run.lease_expires_at == (renewed_at + timedelta(minutes=5)).replace(tzinfo=None)


def test_terminal_update_requires_owner_and_running_state_and_preserves_partial_success(
    database: Database, settings: Settings
) -> None:
    owner = new_owner_token()
    started = datetime.now(UTC)
    fields = lease_claim_fields(owner, now=started)
    run_id = _add_search_run(
        database,
        settings,
        owner_token=owner,
        started_at=started,
        heartbeat_at=fields["heartbeat_at"],
        lease_expires_at=fields["lease_expires_at"],
        lease_version=fields["lease_version"],
    )
    completed_at = datetime.now(UTC)

    with database.session() as session:
        assert finish_owned_run(
            session,
            SearchRun,
            run_id,
            owner,
            {
                "status": "partial",
                "completed_at": completed_at,
                "error_summary": "one provider page failed",
            },
        )
    with database.session() as session:
        assert not finish_owned_run(
            session,
            SearchRun,
            run_id,
            owner,
            {"status": "completed", "completed_at": datetime.now(UTC), "error_summary": None},
        )
        assert not finish_owned_run(
            session,
            SearchRun,
            run_id,
            new_owner_token(),
            {"status": "completed", "completed_at": datetime.now(UTC), "error_summary": None},
        )

    with database.session() as session:
        run = session.get(SearchRun, run_id)
        assert run is not None
        assert run.status == "partial"
        assert run.error_summary == "one provider page failed"
        assert run.completed_at == completed_at.replace(tzinfo=None)


def test_concurrent_terminal_claims_have_exactly_one_winner(
    database: Database, settings: Settings
) -> None:
    owner = new_owner_token()
    started = datetime.now(UTC)
    fields = lease_claim_fields(owner, now=started)
    run_id = _add_search_run(
        database,
        settings,
        owner_token=owner,
        started_at=started,
        heartbeat_at=fields["heartbeat_at"],
        lease_expires_at=fields["lease_expires_at"],
        lease_version=fields["lease_version"],
    )
    barrier = threading.Barrier(2)
    outcomes: list[bool] = []
    errors: list[BaseException] = []

    def finish(status: str) -> None:
        try:
            barrier.wait(timeout=5)
            with database.session() as session:
                outcomes.append(
                    finish_owned_run(
                        session,
                        SearchRun,
                        run_id,
                        owner,
                        {
                            "status": status,
                            "completed_at": datetime.now(UTC),
                        },
                    )
                )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    first = threading.Thread(target=finish, args=("completed",))
    second = threading.Thread(target=finish, args=("failed",))
    first.start()
    second.start()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert sorted(outcomes) == [False, True]
    with database.session() as session:
        run = session.get(SearchRun, run_id)
        assert run is not None
        assert run.status in {"completed", "failed"}
        assert run.completed_at is not None


def test_stale_recovery_compare_and_swap_loses_to_a_concurrent_heartbeat(
    database: Database, settings: Settings
) -> None:
    owner = new_owner_token()
    started = datetime.now(UTC) - timedelta(minutes=30)
    fields = lease_claim_fields(owner, lease_duration=timedelta(minutes=5), now=started)
    run_id = _add_search_run(
        database,
        settings,
        owner_token=owner,
        started_at=started,
        heartbeat_at=fields["heartbeat_at"],
        lease_expires_at=fields["lease_expires_at"],
        lease_version=fields["lease_version"],
    )

    with database.session() as session:
        assert renew_run_lease(
            session,
            SearchRun,
            run_id,
            owner,
            lease_duration=timedelta(minutes=5),
            now=datetime.now(UTC),
        )
        assert not complete_stale_run(
            session,
            SearchRun,
            run_id,
            owner_token=owner,
            lease_version=1,
            values={
                "status": "failed",
                "completed_at": datetime.now(UTC),
                "error_summary": "stale recovery",
            },
        )

    with database.session() as session:
        run = session.get(SearchRun, run_id)
        assert run is not None
        assert run.status == "running"
        assert run.lease_version == 2


def test_lease_expiry_not_wall_clock_start_controls_timeout_and_clock_backward_cases(
    database: Database, settings: Settings
) -> None:
    now = datetime.now(UTC)
    owner = new_owner_token()
    fields = lease_claim_fields(owner, lease_duration=timedelta(minutes=5), now=now)
    timed_out_id = _add_search_run(
        database,
        settings,
        owner_token=owner,
        started_at=now,
        heartbeat_at=now,
        lease_expires_at=now - timedelta(seconds=1),
        lease_version=1,
    )
    future_clock_id = _add_search_run(
        database,
        settings,
        owner_token=new_owner_token(),
        started_at=now + timedelta(hours=1),
        heartbeat_at=now + timedelta(hours=1),
        lease_expires_at=now + timedelta(hours=1, minutes=5),
        lease_version=1,
    )
    del fields

    with database.session() as session:
        timed_out = session.get(SearchRun, timed_out_id)
        future_clock = session.get(SearchRun, future_clock_id)
        assert timed_out is not None
        assert future_clock is not None
        assert run_lease_expired(timed_out, now, fallback_after=timedelta(hours=6))
        assert not run_lease_expired(future_clock, now, fallback_after=timedelta(hours=6))


def test_renewal_and_terminal_writes_refuse_a_stale_claim_after_completion(
    database: Database, settings: Settings
) -> None:
    owner = new_owner_token()
    started = datetime.now(UTC)
    fields = lease_claim_fields(owner, now=started)
    run_id = _add_search_run(
        database,
        settings,
        owner_token=owner,
        started_at=started,
        heartbeat_at=fields["heartbeat_at"],
        lease_expires_at=fields["lease_expires_at"],
        lease_version=fields["lease_version"],
    )
    with database.session() as session:
        assert finish_owned_run(
            session,
            SearchRun,
            run_id,
            owner,
            {"status": "failed", "completed_at": datetime.now(UTC)},
        )
    with database.session() as session:
        assert not renew_run_lease(session, SearchRun, run_id, owner)
        assert not finish_owned_run(
            session,
            SearchRun,
            run_id,
            owner,
            {"status": "completed", "completed_at": datetime.now(UTC)},
        )
    with database.session() as session:
        status = session.scalar(select(SearchRun.status).where(SearchRun.id == run_id))
        assert status == "failed"
