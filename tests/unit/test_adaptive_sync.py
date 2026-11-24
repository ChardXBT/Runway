"""Adaptive per-scope sync scheduling (spec 04 section 3, RT2 part B).

Each connected account refreshes six scopes on their own cadence:
`identity`, `inventory`, `content_metrics_recent`, `content_metrics_older`,
`comments` and `daily_metrics`. `account_sync_state` keeps the last outcome
and next due time per scope; `AdaptiveSyncScheduler` turns due scopes into
`sync_account` jobs carrying the scope, and the sync service runs only the
listed step. A payload without `scope` is a full sync - the pre-RT2 shape.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from runway.api.app import create_app
from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import (
    AccountSyncState,
    ConnectedAccount,
    PlatformContent,
    PublishJob,
    SyncRun,
    WebhookEvent,
    Workspace,
    utcnow,
)
from runway.jobs.queue import Job, JobQueue
from runway.jobs.retention import PURGE_JOB, RetentionPurgeScheduler
from runway.jobs.runtime import build_worker
from runway.jobs.worker import PermanentJobError
from runway.platforms.capabilities import Platform
from runway.platforms.mock import MockPlatformProvider
from runway.platforms.registry import ProviderRegistry
from runway.platforms.sync import (
    SYNC_ACCOUNT_JOB,
    PlatformSyncScheduler,
    SyncService,
    run_sync_account_job,
)
from runway.realtime.processing import PROCESS_WEBHOOK_JOB, run_process_webhook_job
from runway.realtime.schedule import (
    SCOPES,
    AdaptiveSyncScheduler,
    boost,
    effective_interval,
    failure_delay,
    record_scope_run,
)
from runway.realtime.subscriptions import (
    RENEW_SUBSCRIPTION_JOB,
    SUBSCRIBE_APPS_JOB,
    SubscriptionScheduler,
)
from runway.realtime.token_upkeep import REFRESH_TOKEN_JOB, TokenUpkeepScheduler

START = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _account(
    database: Database,
    platform: str = "youtube",
    status: str = "connected",
    external: str = "ext-1",
) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform=platform,
            external_account_id=external,
            status=status,
            granted_scopes_json="[]",
        )
        session.add(account)
        session.flush()
        return int(account.id)


def _active_workspace(database: Database, seen: datetime = START) -> None:
    """Mark workspace 1 as recently used by a human (inactivity floor off)."""
    with database.session() as session:
        workspace = session.get(Workspace, 1)
        assert workspace is not None
        workspace.last_seen_at = seen


def _states(database: Database, account_id: int) -> dict[str, AccountSyncState]:
    with database.session() as session:
        rows = session.scalars(
            select(AccountSyncState).where(AccountSyncState.account_id == account_id)
        ).all()
        for row in rows:
            session.expunge(row)
        return {row.scope: row for row in rows}


def _jobs(database: Database, kind: str | None = None) -> list[PublishJob]:
    with database.session() as session:
        statement = select(PublishJob).order_by(PublishJob.id)
        if kind is not None:
            statement = statement.where(PublishJob.kind == kind)
        rows = session.scalars(statement).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def _latest_run(database: Database, account_id: int) -> SyncRun:
    with database.session() as session:
        run = session.scalars(
            select(SyncRun)
            .where(SyncRun.account_id == account_id)
            .order_by(SyncRun.id.desc())
            .limit(1)
        ).one()
        session.expunge(run)
        return run


@pytest.fixture()
def sync_env(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        platform_sync_max_content_items=200,
    )
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    registry.ensure_mock_accounts()
    service = SyncService(database, settings, registry)
    yield settings, database, registry, service
    database.engine.dispose()


def _mock_account(database: Database, platform: str = "youtube") -> int:
    with database.session() as session:
        account = session.scalar(
            select(ConnectedAccount).where(ConnectedAccount.platform == platform)
        )
        assert account is not None
        return int(account.id)


# ------------------------------------------------------------------- the flag


def test_adaptive_scheduler_replaces_the_fixed_one_when_enabled(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        platform_sync_interval_minutes=30,
        adaptive_sync_enabled=True,
    )
    database = initialize_database(settings)
    try:
        worker = build_worker(settings, database)
        kinds = [type(producer) for producer in worker.producers]
        assert kinds == [
            RetentionPurgeScheduler,
            SubscriptionScheduler,
            TokenUpkeepScheduler,
            AdaptiveSyncScheduler,
        ]
        assert set(worker.handlers) == {
            SYNC_ACCOUNT_JOB,
            PURGE_JOB,
            PROCESS_WEBHOOK_JOB,
            RENEW_SUBSCRIPTION_JOB,
            SUBSCRIBE_APPS_JOB,
            REFRESH_TOKEN_JOB,
        }
    finally:
        database.engine.dispose()


def test_adaptive_sync_disabled_keeps_the_fixed_scheduler(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        platform_sync_interval_minutes=30,
        adaptive_sync_enabled=False,
    )
    database = initialize_database(settings)
    try:
        worker = build_worker(settings, database)
        kinds = [type(producer) for producer in worker.producers]
        assert kinds == [
            RetentionPurgeScheduler,
            SubscriptionScheduler,
            TokenUpkeepScheduler,
            PlatformSyncScheduler,
        ]
    finally:
        database.engine.dispose()


# -------------------------------------------------------------------- produce


def test_produce_enqueues_one_scoped_job_per_scope_and_never_duplicates(
    settings: Settings, database: Database
) -> None:
    account_id = _account(database)
    # An actively used workspace: the inactivity floor does not apply.
    _active_workspace(database)
    queue = JobQueue(database)
    scheduler = AdaptiveSyncScheduler(database, queue, settings)

    scheduler.produce(START)
    jobs = _jobs(database)
    assert len(jobs) == len(SCOPES)
    slot = int(START.timestamp())
    by_scope = {json.loads(job.payload_json)["scope"]: job for job in jobs}
    assert set(by_scope) == set(SCOPES)
    for scope, job in by_scope.items():
        assert job.kind == SYNC_ACCOUNT_JOB
        assert json.loads(job.payload_json) == {
            "account_id": account_id,
            "scope": scope,
            "trigger": "scheduled",
        }
        assert job.idempotency_key == f"{SYNC_ACCOUNT_JOB}:{account_id}:{scope}:{slot}"
        assert job.max_attempts == 1

    # Producing again at the same moment is a no-op: the states were advanced.
    scheduler.produce(START)
    assert len(_jobs(database)) == len(SCOPES)

    states = _states(database, account_id)
    assert set(states) == set(SCOPES)
    assert _aware(states["inventory"].next_run_at) == START + timedelta(minutes=30)
    assert _aware(states["daily_metrics"].next_run_at) == START + timedelta(hours=6)


def test_next_due_reports_the_earliest_scope_time(settings: Settings, database: Database) -> None:
    queue = JobQueue(database)
    scheduler = AdaptiveSyncScheduler(database, queue, settings)
    assert scheduler.next_due(START) is None  # no connected accounts

    account_id = _account(database)
    # A connected account without state rows needs bootstrapping immediately.
    assert scheduler.next_due(START) == START

    scheduler.produce(START)
    earliest = min(_aware(row.next_run_at) for row in _states(database, account_id).values())
    assert scheduler.next_due(START) == earliest


# ------------------------------------------------------------------ the boost


def test_boost_pulls_next_run_forward_and_marks_push(database: Database) -> None:
    account_id = _account(database)
    boost(database, account_id, "inventory", 30, now=START)

    state = _states(database, account_id)["inventory"]
    assert _aware(state.boosted_until) == START + timedelta(minutes=30)
    assert _aware(state.next_run_at) <= START + timedelta(minutes=2)
    assert state.source == "push"


def test_a_verified_webhook_hint_boosts_inventory(settings: Settings, database: Database) -> None:
    account_id = _account(database, platform="tiktok", external="openid-1")
    queue = JobQueue(database)
    with database.session() as session:
        event = WebhookEvent(
            provider="tiktok",
            received_at=utcnow(),
            signature_ok=True,
            dedupe_key="tiktok:test",
            body=b'{"event":"user.profile.updated","user_openid":"openid-1"}',
        )
        session.add(event)
        session.flush()
        queue.enqueue_pending_job(
            session,
            PROCESS_WEBHOOK_JOB,
            idempotency_key=f"webhook:{event.id}",
            payload={"event_id": int(event.id)},
        )
    job = queue.claim("w-test", {PROCESS_WEBHOOK_JOB})
    assert job is not None
    run_process_webhook_job(database, settings, queue, job)

    state = _states(database, account_id)["inventory"]
    assert state.source == "push"
    assert _aware(state.next_run_at) <= utcnow() + timedelta(minutes=2)
    assert _aware(state.boosted_until) > utcnow()
    # The hint still enqueues the full fetch exactly as RT1 did.
    sync_jobs = _jobs(database, SYNC_ACCOUNT_JOB)
    assert len(sync_jobs) == 1
    assert json.loads(sync_jobs[0].payload_json) == {
        "account_id": account_id,
        "trigger": "webhook",
    }


# --------------------------------------------------------------- scoped runs


def test_a_scoped_job_runs_only_that_step_and_records_the_scope(sync_env) -> None:
    _settings, database, _registry, service = sync_env
    account_id = _mock_account(database)
    queue = JobQueue(database)
    job = queue.enqueue(
        SYNC_ACCOUNT_JOB,
        idempotency_key="scoped-inventory",
        payload={"account_id": account_id, "scope": "inventory", "trigger": "scheduled"},
    )
    run_sync_account_job(service, job)

    state = _states(database, account_id)["inventory"]
    assert state.last_status == "completed"
    assert state.source == "poll"
    assert _aware(state.last_run_at) >= utcnow() - timedelta(minutes=1)
    assert _aware(state.next_run_at) > utcnow()
    steps = json.loads(_latest_run(database, account_id).steps_json)
    assert [step["step"] for step in steps] == ["inventory"]
    # Scopes the run did not cover keep their previous (seed-run) state, and
    # content_metrics_older - never part of a full run - has no row at all.
    assert _states(database, account_id).get("content_metrics_older") is None


def test_a_job_without_scope_runs_the_full_sync(sync_env) -> None:
    _settings, database, _registry, service = sync_env
    account_id = _mock_account(database)
    queue = JobQueue(database)
    job = queue.enqueue(
        SYNC_ACCOUNT_JOB,
        idempotency_key="legacy-full",
        payload={"account_id": account_id, "trigger": "webhook"},
    )
    run_sync_account_job(service, job)

    steps = json.loads(_latest_run(database, account_id).steps_json)
    assert [step["step"] for step in steps] == [
        "identity",
        "content",
        "content_metrics",
        "account_daily_metrics",
        "comments",
    ]
    # A full run covers these scopes, so their freshness is recorded;
    # content_metrics_older is not part of a full sync and stays untouched.
    states = _states(database, account_id)
    for scope in ("identity", "inventory", "content_metrics_recent", "comments", "daily_metrics"):
        assert states[scope].last_status == "completed", scope
    assert states.get("content_metrics_older") is None


def test_an_unknown_scope_is_a_permanent_job_error(sync_env) -> None:
    _settings, _database, _registry, service = sync_env
    job = Job(
        id=1,
        kind=SYNC_ACCOUNT_JOB,
        workspace_id=1,
        target_id=None,
        state="running",
        attempts=1,
        max_attempts=1,
        run_at=START,
        idempotency_key="bad-scope",
        payload={"account_id": 1, "scope": "bogus", "trigger": "scheduled"},
        last_error=None,
    )
    with pytest.raises(PermanentJobError):
        run_sync_account_job(service, job)


def test_older_metrics_rotate_through_the_back_catalogue(sync_env) -> None:
    settings, database, registry, service = sync_env
    account_id = _mock_account(database)

    with database.session() as session:
        for i in range(70):
            session.add(
                PlatformContent(
                    account_id=account_id,
                    platform="youtube",
                    external_id=f"older-{i}",
                    content_type="video",
                    published_at=START - timedelta(days=i),
                    metrics_json="{}",
                    raw_json="{}",
                )
            )

    calls: list[list[str]] = []

    class RecordingProvider(MockPlatformProvider):
        def fetch_content_metrics(self, token, external_ids):  # type: ignore[no-untyped-def]
            calls.append(list(external_ids))
            return {}

    registry.set_provider(Platform.youtube, RecordingProvider(Platform.youtube, settings))

    service.run(account_id, scopes=["content_metrics_older"])
    assert len(calls) == 1
    first_batch = calls[0]
    assert len(first_batch) == 50
    # The recent 20 are skipped; the rotation window starts after them.
    assert "older-0" not in first_batch

    service.run(account_id, scopes=["content_metrics_older"])
    assert len(calls) == 2
    assert set(calls[1]).isdisjoint(set(first_batch))


# ------------------------------------------------------------------- backoff


def test_a_failed_scope_backs_off_exponentially_inside_twenty_percent(
    database: Database,
) -> None:
    account_id = _account(database)
    _active_workspace(database)
    normal = timedelta(minutes=30)  # inventory

    record_scope_run(
        database,
        account_id,
        "inventory",
        status="failed",
        error="boom",
        now=START,
        rand=lambda: 0.5,
    )
    state = _states(database, account_id)["inventory"]
    assert state.consecutive_failures == 1
    assert _aware(state.next_run_at) == START + normal * 2

    record_scope_run(
        database,
        account_id,
        "inventory",
        status="failed",
        error="boom",
        now=START,
        rand=lambda: 0.5,
    )
    state = _states(database, account_id)["inventory"]
    assert state.consecutive_failures == 2
    assert _aware(state.next_run_at) == START + normal * 4

    # A success resets the counter; the next failure is back at 2^1, and the
    # jitter lands within +/-20% of that base.
    for rand, expected in (
        (lambda: 0.0, normal * 2 * 0.8),
        (lambda: 1.0, normal * 2 * 1.2),
    ):
        record_scope_run(
            database,
            account_id,
            "inventory",
            status="completed",
            error=None,
            now=START,
            rand=lambda: 0.5,
        )
        record_scope_run(
            database,
            account_id,
            "inventory",
            status="failed",
            error="boom",
            now=START,
            rand=rand,
        )
        state = _states(database, account_id)["inventory"]
        assert state.consecutive_failures == 1
        assert _aware(state.next_run_at) == START + expected


def test_backoff_caps_at_one_day_and_honours_retry_after(database: Database) -> None:
    account_id = _account(database)
    _active_workspace(database)
    for _ in range(9):
        record_scope_run(
            database,
            account_id,
            "inventory",
            status="failed",
            error="x",
            now=START,
            rand=lambda: 0.5,
        )
    state = _states(database, account_id)["inventory"]
    assert _aware(state.next_run_at) == START + timedelta(hours=24)

    record_scope_run(
        database,
        account_id,
        "inventory",
        status="failed",
        error="quota",
        now=START,
        retry_after=3600.0,
        rand=lambda: 0.5,
    )
    state = _states(database, account_id)["inventory"]
    assert _aware(state.next_run_at) == START + timedelta(hours=1)


def test_failure_delay_helper_covers_jitter_and_cap() -> None:
    normal = timedelta(minutes=15)
    assert failure_delay(normal, 1, rand=lambda: 0.5) == normal * 2
    assert failure_delay(normal, 1, rand=lambda: 0.0) == normal * 2 * 0.8
    assert failure_delay(normal, 1, rand=lambda: 1.0) == normal * 2 * 1.2
    assert failure_delay(normal, 20, rand=lambda: 0.5) == timedelta(hours=24)
    assert failure_delay(normal, 3, retry_after=42.0) == timedelta(seconds=42)


# ----------------------------------------------------------------- inactivity


def test_fourteen_inactive_days_drop_every_scope_to_daily() -> None:
    stale = START - timedelta(days=15)
    for scope in SCOPES:
        assert effective_interval(scope, None, stale, START) >= timedelta(hours=24)
        # A boost cannot beat the inactivity floor.
        boosted = START + timedelta(minutes=30)
        assert effective_interval(scope, boosted, stale, START) >= timedelta(hours=24)
    # An active workspace keeps the normal and boosted cadences.
    fresh = START - timedelta(minutes=5)
    assert effective_interval("inventory", None, fresh, START) == timedelta(minutes=30)
    assert effective_interval(
        "inventory", START + timedelta(minutes=30), fresh, START
    ) == timedelta(minutes=2)


def test_produce_on_an_inactive_workspace_schedules_scopes_a_day_out(
    settings: Settings, database: Database
) -> None:
    account_id = _account(database)
    _active_workspace(database, seen=START - timedelta(days=15))
    scheduler = AdaptiveSyncScheduler(database, JobQueue(database), settings)
    scheduler.produce(START)
    for state in _states(database, account_id).values():
        assert _aware(state.next_run_at) >= START + timedelta(hours=24)


def test_human_requests_touch_the_workspace_at_most_once_per_ten_minutes(
    tmp_path: Path,
) -> None:
    settings = Settings(data_dir=tmp_path / "data", platform_provider="mock")
    database = initialize_database(settings)
    application = create_app(settings)
    client = TestClient(application, base_url="http://127.0.0.1:8000")
    try:
        assert client.get("/api/settings").status_code == 200
        with database.session() as session:
            workspace = session.get(Workspace, 1)
            assert workspace is not None and workspace.last_seen_at is not None

        # The throttle allows one write per ten minutes; a second request
        # must not overwrite the value.
        with database.session() as session:
            workspace = session.get(Workspace, 1)
            assert workspace is not None
            workspace.last_seen_at = START
        assert client.get("/api/settings").status_code == 200
        with database.session() as session:
            workspace = session.get(Workspace, 1)
            assert workspace is not None
            assert _aware(workspace.last_seen_at) == START

        writes = application.state.workspace_seen_writes
        writes[1] -= 700  # pretend the last write was 11+ minutes ago
        assert client.get("/api/settings").status_code == 200
        with database.session() as session:
            workspace = session.get(Workspace, 1)
            assert workspace is not None
            assert _aware(workspace.last_seen_at) > START
    finally:
        database.engine.dispose()
        application.state.database.engine.dispose()


# ------------------------------------------------------- scoped run via worker


def test_the_worker_produces_scoped_jobs_and_the_handler_runs_them(sync_env) -> None:
    settings, database, _registry, _service = sync_env
    youtube_id = _mock_account(database, "youtube")
    tiktok_id = _mock_account(database, "tiktok")
    # The mock seed's full sync already scheduled the five covered scopes;
    # clear every state so all six are due in this produce.
    with database.session() as session:
        for state in session.scalars(select(AccountSyncState)).all():
            session.delete(state)
    worker = build_worker(settings, database)
    worker.worker_id = "w-test"

    worker.produce()
    produced = _jobs(database, SYNC_ACCOUNT_JOB)
    assert produced, "the adaptive producer enqueued nothing"
    assert all(json.loads(job.payload_json)["trigger"] == "scheduled" for job in produced)
    youtube_scopes = {
        json.loads(job.payload_json)["scope"]
        for job in produced
        if json.loads(job.payload_json)["account_id"] == youtube_id
    }
    assert youtube_scopes == set(SCOPES)

    while worker.run_once():
        pass
    states = _states(database, youtube_id)
    assert all(state.last_status == "completed" for state in states.values())
    for job in _jobs(database, SYNC_ACCOUNT_JOB):
        assert job.state == "completed"
    # TikTok has no comments capability on this API; the scope is skipped, not failed.
    assert _states(database, tiktok_id)["comments"].last_status == "skipped"
