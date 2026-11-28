"""publish_jobs queue, the worker loop and the scheduled-sync producer."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event, select
from sqlalchemy.dialects import postgresql, sqlite

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ConnectedAccount, PublishJob, SyncRun
from runway.jobs.queue import (
    JOB_STATE_MACHINE,
    RECONCILE,
    SEND,
    Job,
    JobQueue,
    JobState,
    LeaseLost,
    claim_statement,
    retry_delay,
)
from runway.jobs.retention import PURGE_JOB, RetentionPurgeScheduler
from runway.jobs.runtime import build_worker
from runway.jobs.worker import PermanentJobError, Worker
from runway.platforms.registry import ProviderRegistry
from runway.platforms.sync import SYNC_ACCOUNT_JOB, PlatformSyncScheduler
from runway.realtime.processing import PROCESS_WEBHOOK_JOB
from runway.realtime.subscriptions import (
    RENEW_SUBSCRIPTION_JOB,
    SUBSCRIBE_APPS_JOB,
    SubscriptionScheduler,
)
from runway.realtime.token_upkeep import REFRESH_TOKEN_JOB, TokenUpkeepScheduler

START = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self, now: datetime = START) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def queue(database: Database, clock: Clock) -> JobQueue:
    return JobQueue(database, clock=clock)


def _row(database: Database, job_id: int) -> PublishJob:
    with database.session() as session:
        row = session.get(PublishJob, job_id)
        assert row is not None
        session.expunge(row)
        return row


# ----------------------------------------------------------------- enqueue


def test_enqueue_is_idempotent_on_the_key(queue: JobQueue, database: Database) -> None:
    first = queue.enqueue("probe_media", idempotency_key="k1", payload={"a": 1})
    second = queue.enqueue("probe_media", idempotency_key="k1", payload={"a": 2})

    assert second.id == first.id
    assert second.payload == {"a": 1}
    with database.session() as session:
        assert len(session.scalars(select(PublishJob)).all()) == 1
    row = _row(database, first.id)
    assert row.state == "pending"
    assert row.workspace_id == 1
    assert row.target_id is None


def test_concurrent_enqueues_of_one_key_create_one_job(queue: JobQueue, database: Database) -> None:
    barrier = threading.Barrier(4)
    ids: list[int] = []

    def enqueue() -> None:
        barrier.wait()
        ids.append(queue.enqueue("purge", idempotency_key="same").id)

    threads = [threading.Thread(target=enqueue) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(set(ids)) == 1 and len(ids) == 4


# ------------------------------------------------------------------- claim


def test_claim_takes_the_earliest_due_job_of_a_handled_kind(queue: JobQueue, clock: Clock) -> None:
    later = queue.enqueue("purge", idempotency_key="later", run_at=START + timedelta(minutes=5))
    due = queue.enqueue("purge", idempotency_key="due", run_at=START - timedelta(minutes=1))
    queue.enqueue("unhandled", idempotency_key="other", run_at=START - timedelta(hours=1))

    claimed = queue.claim("w1", {"purge"}, lease_seconds=60)
    assert claimed is not None and claimed.id == due.id
    assert claimed.attempts == 1
    # The only other purge job is not due yet; the unhandled kind is never claimed.
    assert queue.claim("w1", {"purge"}) is None
    clock.advance(minutes=5)
    again = queue.claim("w1", {"purge"})
    assert again is not None and again.id == later.id


def test_sqlite_claim_runs_inside_begin_immediate(queue: JobQueue, database: Database) -> None:
    statements: list[str] = []

    def record(_conn, _cursor, statement, *_args) -> None:  # type: ignore[no-untyped-def]
        statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", record)
    try:
        queue.enqueue("purge", idempotency_key="k")
        statements.clear()
        assert queue.claim("w1", {"purge"}) is not None
    finally:
        event.remove(database.engine, "before_cursor_execute", record)
    assert statements[0] == "BEGIN IMMEDIATE"
    assert not any("FOR UPDATE" in statement for statement in statements)


def test_postgres_claim_uses_for_update_skip_locked() -> None:
    statement = claim_statement("postgresql", START, {"send", "verify"})
    sql = str(statement.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "publish_jobs.state = %(state_1)s" in sql
    assert "publish_jobs.run_at <= %(run_at_1)s" in sql
    assert "ORDER BY publish_jobs.run_at, publish_jobs.id" in sql
    assert "LIMIT %(param_1)s" in sql
    lite = str(claim_statement("sqlite", START, {"send"}).compile(dialect=sqlite.dialect()))
    assert "FOR UPDATE" not in lite


# ---------------------------------------------------------- complete / fail


def test_complete_requires_the_lease_holder(queue: JobQueue, database: Database) -> None:
    job = queue.enqueue("purge", idempotency_key="k")
    assert queue.claim("w1", {"purge"}) is not None
    with pytest.raises(LeaseLost):
        queue.complete(job.id, "w2")
    queue.complete(job.id, "w1")
    row = _row(database, job.id)
    assert row.state == "completed"
    assert row.locked_by is None and row.locked_until is None
    with pytest.raises(LeaseLost):
        queue.complete(job.id, "w1")


def test_failures_retry_with_backoff_until_max_attempts(
    queue: JobQueue, database: Database, clock: Clock
) -> None:
    job = queue.enqueue("purge", idempotency_key="k", max_attempts=3)
    delays = []
    for attempt in (1, 2):
        claimed = queue.claim("w1", {"purge"})
        assert claimed is not None and claimed.attempts == attempt
        assert queue.fail(job.id, "w1", "boom") is JobState.PENDING
        row = _row(database, job.id)
        delays.append(row.run_at.replace(tzinfo=UTC) - clock.now)
        assert queue.claim("w1", {"purge"}) is None  # backing off
        clock.now = row.run_at.replace(tzinfo=UTC)
    assert delays == [timedelta(minutes=1), timedelta(minutes=5)]

    assert queue.claim("w1", {"purge"}) is not None
    assert queue.fail(job.id, "w1", "boom again") is JobState.FAILED
    row = _row(database, job.id)
    assert row.state == "failed" and row.attempts == 3 and row.last_error == "boom again"
    clock.advance(days=1)
    assert queue.claim("w1", {"purge"}) is None


def test_a_permanent_failure_is_not_retried(queue: JobQueue) -> None:
    job = queue.enqueue("purge", idempotency_key="k", max_attempts=5)
    assert queue.claim("w1", {"purge"}) is not None
    assert queue.fail(job.id, "w1", "bad payload", retryable=False) is JobState.FAILED


def test_retry_delay_is_bounded() -> None:
    assert [retry_delay(n).total_seconds() for n in (1, 2, 3, 4, 10)] == [
        60,
        300,
        1200,
        1200,
        1200,
    ]


# ------------------------------------------------------------ expired leases


def test_an_expired_lease_on_an_ordinary_job_is_run_again(
    queue: JobQueue, database: Database, clock: Clock
) -> None:
    job = queue.enqueue("purge", idempotency_key="k")
    assert queue.claim("dead-worker", {"purge"}, lease_seconds=60) is not None
    clock.advance(minutes=2)

    assert queue.claim("w2", {"purge"}) is None  # recovered, but backing off
    row = _row(database, job.id)
    assert row.state == "pending" and row.locked_by is None
    clock.advance(minutes=1)
    reclaimed = queue.claim("w2", {"purge"})
    assert reclaimed is not None and reclaimed.id == job.id and reclaimed.attempts == 2
    with pytest.raises(LeaseLost):
        queue.complete(job.id, "dead-worker")


def test_an_expired_send_is_never_rerun_but_handed_to_reconcile(
    queue: JobQueue, database: Database, clock: Clock
) -> None:
    send = queue.enqueue(SEND, idempotency_key="target:1:hash", max_attempts=3)
    assert queue.claim("dead-worker", {SEND}, lease_seconds=60) is not None
    clock.advance(minutes=2)

    assert queue.claim("w2", {SEND}) is None
    assert _row(database, send.id).state == JobState.RECONCILING.value
    clock.advance(hours=1)
    assert queue.claim("w2", {SEND}) is None  # still never re-run
    with database.session() as session:
        reconcile = session.scalar(select(PublishJob).where(PublishJob.kind == RECONCILE))
        assert reconcile is not None
        assert reconcile.state == "pending"
        assert json.loads(reconcile.payload_json) == {"send_job_id": send.id}
        assert reconcile.idempotency_key == f"reconcile:job:{send.id}"


def test_the_reconcile_job_gets_the_read_job_retry_budget_not_the_sends(
    queue: JobQueue, database: Database, clock: Clock
) -> None:
    # A send job runs once (SEND_MAX_ATTEMPTS = 1); the reconcile job it leaves
    # behind only reads, so it must get the read-job budget, not copy the 1.
    queue.enqueue(SEND, idempotency_key="target:1:hash", max_attempts=1)
    assert queue.claim("dead-worker", {SEND}, lease_seconds=60) is not None
    clock.advance(minutes=2)

    assert queue.claim("w2", {SEND}) is None
    with database.session() as session:
        reconcile = session.scalar(select(PublishJob).where(PublishJob.kind == RECONCILE))
        assert reconcile is not None
        assert reconcile.max_attempts == 3


def test_the_reconcile_retry_budget_matches_the_read_job_budget() -> None:
    # queue.py must not import runway.posts; keep the constant in lock-step by test.
    from runway.jobs.queue import RECONCILE_MAX_ATTEMPTS
    from runway.posts.jobs import READ_JOB_MAX_ATTEMPTS

    assert RECONCILE_MAX_ATTEMPTS == READ_JOB_MAX_ATTEMPTS == 3


def test_the_expired_send_hook_is_replaceable(database: Database, clock: Clock) -> None:
    seen: list[int] = []

    def hook(session, job, now) -> None:  # type: ignore[no-untyped-def]
        seen.append(job.id)
        JOB_STATE_MACHINE.apply(job, JobState.FAILED)
        job.locked_by = None

    queue = JobQueue(database, clock=clock, expired_send_handler=hook)
    send = queue.enqueue(SEND, idempotency_key="s")
    queue.claim("dead", {SEND}, lease_seconds=1)
    clock.advance(seconds=5)
    queue.claim("w2", {SEND})
    assert seen == [send.id]


# ------------------------------------------------------------------ worker


def _worker(queue: JobQueue, handlers, **kwargs) -> Worker:  # type: ignore[no-untyped-def]
    return Worker(queue=queue, handlers=handlers, worker_id="w-test", **kwargs)


def test_worker_runs_a_due_job_and_completes_it(queue: JobQueue, database: Database) -> None:
    ran: list[Job] = []
    job = queue.enqueue("purge", idempotency_key="k", payload={"x": 1})
    worker = _worker(queue, {"purge": ran.append})

    assert worker.run_once() is True
    assert [item.payload for item in ran] == [{"x": 1}]
    assert _row(database, job.id).state == "completed"
    assert worker.run_once() is False


def test_worker_records_handler_errors(queue: JobQueue, database: Database) -> None:
    def broken(_job: Job) -> None:
        raise RuntimeError("token=abc123 leaked?")

    def invalid(_job: Job) -> None:
        raise PermanentJobError("no account")

    retry = queue.enqueue("broken", idempotency_key="a")
    final = queue.enqueue("invalid", idempotency_key="b")
    worker = _worker(queue, {"broken": broken, "invalid": invalid})
    worker.run_once()
    worker.run_once()

    retry_row = _row(database, retry.id)
    assert retry_row.state == "pending"
    assert "abc123" not in (retry_row.last_error or "")
    assert _row(database, final.id).state == "failed"


def test_unknown_pending_counts_group_edge_case_kinds_without_touching_state(
    queue: JobQueue, database: Database
) -> None:
    queue.enqueue("purge", idempotency_key="known")
    queue.enqueue("", idempotency_key="empty")
    queue.enqueue("unknown/υνικοde", idempotency_key="unicode")
    queue.enqueue("future_kind", idempotency_key="future-1")
    queue.enqueue("future_kind", idempotency_key="future-2", run_at=START + timedelta(days=1))
    finished = queue.enqueue("future_kind", idempotency_key="finished")
    with database.session() as session:
        row = session.get(PublishJob, finished.id)
        assert row is not None
        row.state = "completed"

    assert list(queue.unknown_pending_counts({"purge"}).items()) == [
        ("", 1),
        ("future_kind", 2),
        ("unknown/υνικοde", 1),
    ]
    assert queue.unknown_pending_counts({"purge", ""})["future_kind"] == 2
    assert queue.unknown_pending_counts(set())["purge"] == 1
    assert _row(database, finished.id).state == "completed"


def test_unknown_pending_job_is_operator_visible_once_per_worker_and_survives_restart(
    queue: JobQueue, database: Database, caplog: pytest.LogCaptureFixture
) -> None:
    unknown = queue.enqueue(
        "future_kind", idempotency_key="unknown", run_at=START - timedelta(minutes=1)
    )
    before = _row(database, unknown.id)
    worker = _worker(queue, {"purge": lambda _job: None}, unknown_kind_check_seconds=0)

    with caplog.at_level(logging.WARNING, logger="runway.jobs.worker"):
        for _ in range(5):
            assert worker.run_once() is False

    warnings = [record for record in caplog.records if "does not handle" in record.message]
    assert len(warnings) == 1
    assert "future_kind" in warnings[0].message
    assert "rolling-deploy compatibility" in warnings[0].message
    after = _row(database, unknown.id)
    assert (after.state, after.attempts, after.locked_by, after.locked_until) == (
        "pending",
        0,
        None,
        None,
    )
    assert after.run_at == before.run_at

    caplog.clear()
    restarted = _worker(queue, {"purge": lambda _job: None}, unknown_kind_check_seconds=0)
    with caplog.at_level(logging.WARNING, logger="runway.jobs.worker"):
        assert restarted.run_once() is False
    restarted_warnings = [
        record for record in caplog.records if "does not handle" in record.message
    ]
    assert len(restarted_warnings) == 1
    assert _row(database, unknown.id).state == "pending"


def test_idle_sleep_waits_for_the_earliest_job_capped_at_30_seconds(
    queue: JobQueue, clock: Clock
) -> None:
    worker = _worker(queue, {"purge": lambda _job: None})
    assert worker.idle_seconds() == 30
    queue.enqueue("purge", idempotency_key="soon", run_at=START + timedelta(seconds=7))
    assert worker.idle_seconds() == 7
    queue.enqueue("purge", idempotency_key="due", run_at=START - timedelta(seconds=7))
    assert worker.idle_seconds() == 0
    clock.advance(hours=-1)
    assert worker.idle_seconds() == 30


def test_worker_stops_promptly_when_asked(queue: JobQueue) -> None:
    worker = _worker(queue, {"purge": lambda _job: None})
    thread = threading.Thread(target=worker.run_forever)
    thread.start()
    worker.stop()
    thread.join(timeout=10)
    assert not thread.is_alive()


def test_the_idle_sleep_waits_in_short_slices_so_a_signal_can_stop_it(queue: JobQueue) -> None:
    # A signal handler runs only between waits on the main thread; one 30 s
    # Event.wait would hold the launcher's stop request for up to 30 s.
    waits: list[float | None] = []

    class RecordingEvent(threading.Event):
        def wait(self, timeout: float | None = None) -> bool:
            waits.append(timeout)
            if len(waits) == 3:
                self.set()
            return super().wait(0)

    worker = _worker(queue, {"purge": lambda _job: None})
    worker.stop_event = RecordingEvent()
    worker.sleep(30)
    assert len(waits) == 3
    assert all(timeout is not None and timeout <= 0.5 for timeout in waits)


# ------------------------------------------------------ scheduled platform sync


def _connected_account(database: Database, external: str, status: str = "connected") -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform="youtube",
            external_account_id=external,
            status=status,
            granted_scopes_json="[]",
        )
        session.add(account)
        session.flush()
        return account.id


def test_scheduler_produces_one_sync_job_per_account_per_interval(
    queue: JobQueue, database: Database, clock: Clock
) -> None:
    first = _connected_account(database, "a")
    _connected_account(database, "b", status="disconnected")
    scheduler = PlatformSyncScheduler(database, queue, interval_minutes=60)

    scheduler.produce(clock.now)
    scheduler.produce(clock.now + timedelta(minutes=10))
    with database.session() as session:
        jobs = session.scalars(select(PublishJob)).all()
        assert [(job.kind, json.loads(job.payload_json)["account_id"]) for job in jobs] == [
            (SYNC_ACCOUNT_JOB, first)
        ]
    assert scheduler.next_due(clock.now) == START + timedelta(hours=1)

    scheduler.produce(clock.now + timedelta(hours=1))
    with database.session() as session:
        assert len(session.scalars(select(PublishJob)).all()) == 2


def test_the_worker_runs_scheduled_sync_with_the_existing_sync_code(tmp_path, clock: Clock) -> None:
    from runway.db import initialize_database

    # The legacy fixed-slot path, kept behind adaptive_sync_enabled=false.
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        platform_sync_interval_minutes=30,
        adaptive_sync_enabled=False,
    )
    database = initialize_database(settings)
    try:
        ProviderRegistry(database, settings).ensure_mock_accounts()
        with database.session() as session:
            accounts = session.scalars(select(ConnectedAccount.id)).all()
            seeded_runs = len(session.scalars(select(SyncRun)).all())
        worker = build_worker(settings, database)
        worker.worker_id = "w-test"

        worker.produce()
        while worker.run_once():
            pass

        with database.session() as session:
            runs = session.scalars(select(SyncRun).where(SyncRun.trigger == "scheduled")).all()
            assert len(session.scalars(select(SyncRun)).all()) == seeded_runs + len(accounts)
            jobs = session.scalars(select(PublishJob)).all()
        assert sorted(run.account_id for run in runs) == sorted(accounts)
        assert {run.status for run in runs} == {"completed"}
        assert {job.state for job in jobs} == {"completed"}
    finally:
        database.engine.dispose()


def test_no_sync_producer_when_the_interval_is_zero(settings: Settings, database: Database) -> None:
    # Legacy path only: with adaptive sync enabled (the default) the
    # AdaptiveSyncScheduler produces scoped jobs instead.
    worker = build_worker(settings.model_copy(update={"adaptive_sync_enabled": False}), database)
    from runway.agent.webhooks import DELIVER_WEBHOOK_JOB, WebhookDispatcher
    from runway.posts.jobs import PUBLISHING_JOB_KINDS

    # Retention purge always runs; token upkeep renews Instagram long-lived
    # tokens whether or not scheduled sync is on (spec 04 section 2).
    assert [type(producer) for producer in worker.producers] == [
        RetentionPurgeScheduler,
        WebhookDispatcher,
        SubscriptionScheduler,
        TokenUpkeepScheduler,
    ]
    assert (
        set(worker.handlers)
        == {
            SYNC_ACCOUNT_JOB,
            PURGE_JOB,
            DELIVER_WEBHOOK_JOB,
            PROCESS_WEBHOOK_JOB,
            RENEW_SUBSCRIPTION_JOB,
            SUBSCRIBE_APPS_JOB,
            REFRESH_TOKEN_JOB,
        }
        | PUBLISHING_JOB_KINDS
    )


def test_hourly_purge_removes_only_expired_keys_and_events(
    queue: JobQueue, database: Database, clock: Clock
) -> None:
    from sqlalchemy import update

    from runway.db.models import EventRecord, IdempotencyKey, utcnow
    from runway.events.log import record_event
    from runway.events.types import SYNC_FINISHED
    from runway.jobs.retention import run_purge_job

    now = utcnow()
    with database.session() as session:
        old_event = record_event(session, 1, SYNC_FINISHED, "account", 1).id
        fresh_event = record_event(session, 1, SYNC_FINISHED, "account", 2).id
        session.execute(
            update(EventRecord)
            .where(EventRecord.id == old_event)
            .values(created_at=now - timedelta(days=8))
        )
        for key, age in (("old", timedelta(hours=25)), ("fresh", timedelta(hours=1))):
            session.add(
                IdempotencyKey(
                    workspace_id=1,
                    principal_id="user:local-owner",
                    key=key,
                    request_hash="0" * 64,
                    response_status=200,
                    response_body="{}",
                    created_at=now - age,
                )
            )

    scheduler = RetentionPurgeScheduler(queue)
    scheduler.produce(clock.now)
    scheduler.produce(clock.now + timedelta(minutes=30))
    assert scheduler.next_due(clock.now) == START + timedelta(hours=1)
    job = queue.claim("w1", {PURGE_JOB})
    assert job is not None
    assert queue.claim("w1", {PURGE_JOB}) is None  # one job per hour slot

    run_purge_job(database, database.settings, job)

    with database.session() as session:
        assert list(session.scalars(select(EventRecord.id))) == [fresh_event]
        assert list(session.scalars(select(IdempotencyKey.key))) == ["fresh"]


# ------------------------------------------------------- review fixes (item 6)


def test_a_corrupt_job_row_is_failed_once_and_the_next_job_still_runs(
    queue: JobQueue, database: Database
) -> None:
    corrupt = queue.enqueue("purge", idempotency_key="corrupt", run_at=START - timedelta(minutes=2))
    good = queue.enqueue("purge", idempotency_key="good", run_at=START - timedelta(minutes=1))
    with database.session() as session:
        row = session.get(PublishJob, corrupt.id)
        assert row is not None
        row.payload_json = "{not json"

    claimed = queue.claim("w1", {"purge"})

    assert claimed is not None and claimed.id == good.id
    failed = _row(database, corrupt.id)
    assert failed.state == "failed"
    assert "cannot be decoded" in (failed.last_error or "")
    assert failed.locked_by is None
    # Never offered again.
    queue.complete(good.id, "w1")
    assert queue.claim("w1", {"purge"}) is None


def test_a_non_object_payload_is_undecodable(queue: JobQueue, database: Database) -> None:
    job = queue.enqueue("purge", idempotency_key="list")
    with database.session() as session:
        row = session.get(PublishJob, job.id)
        assert row is not None
        row.payload_json = "[1, 2]"
    assert queue.claim("w1", {"purge"}) is None
    assert _row(database, job.id).state == "failed"


def test_the_worker_survives_a_failing_idle_query_and_backs_off(
    queue: JobQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[int] = []
    worker = _worker(queue, {"purge": lambda job: ran.append(job.id)}, max_idle_seconds=0.05)
    calls = {"n": 0}
    real_next_run_at = queue.next_run_at

    def flaky_next_run_at(kinds):  # type: ignore[no-untyped-def]
        calls["n"] += 1
        if calls["n"] <= 3:
            raise RuntimeError("database is locked")
        return real_next_run_at(kinds)

    monkeypatch.setattr(queue, "next_run_at", flaky_next_run_at)
    thread = threading.Thread(target=worker.run_forever)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while calls["n"] <= 3 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert thread.is_alive(), "an idle-query failure killed the worker"
        job = queue.enqueue("purge", idempotency_key="after-failures")
        while not ran and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ran == [job.id]
    finally:
        worker.stop()
        thread.join(timeout=10)
    assert not thread.is_alive()


def test_failure_backoff_grows_and_is_capped(queue: JobQueue) -> None:
    worker = _worker(queue, {})
    assert [worker.failure_backoff(n) for n in (1, 2, 3, 4, 5, 6, 50)] == [
        1,
        2,
        4,
        8,
        16,
        30,
        30,
    ]


# ------------------------------------------------------- review fixes (item 9)


def test_purge_removes_old_finished_jobs_but_keeps_publishing_history_longer(
    queue: JobQueue, database: Database, settings: Settings
) -> None:
    from sqlalchemy import update

    from runway.db.models import utcnow
    from runway.jobs.retention import purge_finished_jobs

    now = utcnow()
    ages = {
        ("sync_account", "completed", 15): "removed",
        ("sync_account", "failed", 15): "removed",
        ("sync_account", "completed", 13): "kept",
        ("sync_account", "pending", 40): "kept",  # unfinished work is never purged
        ("send", "completed", 30): "kept",
        ("reconcile", "failed", 30): "kept",
        ("send", "completed", 91): "removed",
        ("send", "reconciling", 200): "kept",
    }
    ids: dict[tuple[str, str, int], int] = {}
    for kind, state, days in ages:
        job = queue.enqueue(kind, idempotency_key=f"{kind}:{state}:{days}")
        ids[(kind, state, days)] = job.id
        with database.session() as session:
            session.execute(
                update(PublishJob)
                .where(PublishJob.id == job.id)
                .values(state=state, updated_at=now - timedelta(days=days))
            )

    with database.session() as session:
        assert purge_finished_jobs(session, now=now) == 3

    with database.session() as session:
        remaining = set(session.scalars(select(PublishJob.id)))
    for key, expected in ages.items():
        assert (ids[key] in remaining) is (expected == "kept"), key


def test_purge_removes_only_unreferenced_unheld_lock_files(
    database: Database, settings: Settings
) -> None:
    from runway.db.models import ConnectedAccount
    from runway.jobs.retention import run_purge_job
    from runway.locks import exclusive_file_lock
    from runway.platforms.sync import sync_lock_path
    from runway.platforms.tokens import TokenStore

    with database.session() as session:
        account = ConnectedAccount(
            platform="youtube",
            external_account_id="UC-live",
            status="connected",
            granted_scopes_json="[]",
            token_ref="liveref",
        )
        session.add(account)
        session.flush()
        live_id = account.id
    refresh_dir = TokenStore(settings.resolved_data_dir / "secrets").refresh_lock_dir
    refresh_dir.mkdir(parents=True)
    for name in ("liveref", "goneref", "heldref"):
        (refresh_dir / f"{name}.lock").write_bytes(b"\0")
    sync_dir = sync_lock_path(settings, live_id).parent
    sync_dir.mkdir(parents=True)
    sync_lock_path(settings, live_id).write_bytes(b"\0")
    sync_lock_path(settings, 9999).write_bytes(b"\0")

    job = Job.of(
        PublishJob(
            id=1,
            kind="purge",
            workspace_id=1,
            target_id=None,
            state="running",
            attempts=1,
            max_attempts=1,
            run_at=START,
            idempotency_key="purge:test",
            payload_json="{}",
            last_error=None,
        )
    )
    with exclusive_file_lock(refresh_dir / "heldref.lock", timeout=0):
        run_purge_job(database, settings, job)

    assert sorted(path.stem for path in refresh_dir.glob("*.lock")) == ["heldref", "liveref"]
    assert [path.stem for path in sync_dir.glob("*.lock")] == [f"account-{live_id}"]


def test_a_standby_machine_claims_no_job(queue: JobQueue, database: Database) -> None:
    queue.enqueue("probe", idempotency_key="standby-1")
    ran: list[int] = []
    worker = _worker(queue, {"probe": lambda job: ran.append(job.id)}, is_active=lambda: False)

    assert worker.run_once() is False
    assert ran == []
    with database.session() as session:
        assert session.scalar(select(PublishJob.state)) == "pending"


def test_a_job_that_finds_standby_after_marking_busy_is_deferred_not_run(
    queue: JobQueue, database: Database, clock: Clock
) -> None:
    from contextlib import contextmanager

    job = queue.enqueue("probe", idempotency_key="standby-2")
    answers = iter([True, False])
    events: list[str] = []

    @contextmanager
    def busy(kind: str):  # type: ignore[no-untyped-def]
        events.append(f"busy {kind}")
        yield
        events.append("idle")

    worker = _worker(
        queue,
        {"probe": lambda job: events.append("ran")},
        is_active=lambda: next(answers),
        busy=busy,
    )

    assert worker.run_once() is False
    assert events == ["busy probe", "idle"]
    row = _row(database, job.id)
    assert (row.state, row.attempts) == ("pending", 0)
    assert row.run_at.replace(tzinfo=UTC) > clock.now


def test_an_active_machine_runs_the_job_inside_its_busy_marker(queue: JobQueue) -> None:
    from contextlib import contextmanager

    queue.enqueue("probe", idempotency_key="standby-3")
    events: list[str] = []

    @contextmanager
    def busy(kind: str):  # type: ignore[no-untyped-def]
        events.append("busy")
        yield
        events.append("idle")

    worker = _worker(
        queue, {"probe": lambda job: events.append("ran")}, is_active=lambda: True, busy=busy
    )
    assert worker.run_once() is True
    assert events == ["busy", "ran", "idle"]
