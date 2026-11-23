"""The publishing worker: exactly-once sending, crashes at every step, retries, reconcile.

A fake adapter records every call against a fake "platform" that holds the
posts it created. A crash is a `BaseException` raised inside the handler, the
way a killed process stops mid-call: nothing after it runs, the job keeps its
lease, and a fresh worker (new queue, engine and registry instances on the
same database) recovers once the lease has expired.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from runway.auth.principal import local_owner
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    AccountPublisher,
    ConnectedAccount,
    DraftTarget,
    EventRecord,
    PublishingControl,
    PublishJob,
    TargetAttempt,
)
from runway.jobs.queue import JobQueue
from runway.jobs.worker import Worker
from runway.platforms.assisted_publisher import AssistedPublisher
from runway.platforms.capabilities import Platform
from runway.platforms.publisher import (
    Issue,
    PollResult,
    Progress,
    PublishContext,
    PublishRetryLater,
    ReconcileResult,
    SendResult,
    VerifyResult,
)
from runway.platforms.publisher_registry import PublisherRegistry, build_publisher_registry
from runway.posts.engine import PublishingEngine
from runway.posts.schemas import PostCreate, ReleaseRequest, TargetInput
from runway.posts.service import PostService

START = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


class SimulatedCrash(BaseException):  # noqa: N818 - the process "dies" here
    pass


@dataclass
class FakePlatform:
    """What the fake platform holds: created posts by target id."""

    posts: dict[int, str] = field(default_factory=dict)
    creates: list[int] = field(default_factory=list)


@dataclass
class FakePublisher:
    platform_state: FakePlatform
    name: str = "fake_direct"
    kind: str = "direct"
    platform: Platform = Platform.youtube
    crash_at: str | None = None
    fail_with: BaseException | None = None
    create_before_failing: bool = False
    prepare_lead: timedelta = timedelta(0)
    calls: list[str] = field(default_factory=list)
    resumed_from: list[Mapping[str, Any]] = field(default_factory=list)

    def supports(self, post_type: str) -> bool:
        return post_type == "youtube_post"

    def _crash(self, point: str) -> None:
        if self.crash_at == point:
            self.crash_at = None
            raise SimulatedCrash(point)

    def preflight(self, ctx: PublishContext) -> list[Issue]:
        self.calls.append("preflight")
        if ctx.attempt_id is not None:
            self._crash("after_intent")
        return []

    def prepare(self, ctx: PublishContext) -> Mapping[str, Any]:
        self.calls.append("prepare")
        return {"container": f"c-{ctx.target_id}"}

    def send(self, ctx: PublishContext, progress: Progress) -> SendResult:
        self.calls.append("send")
        self.resumed_from.append(dict(ctx.resumable))
        offset = int(ctx.resumable.get("offset", 0))
        for chunk in range(offset, 2):
            self.calls.append(f"chunk{chunk}")
            ctx.save_resumable({**ctx.resumable, "offset": chunk + 1})
            progress.report(chunk + 1, 3, stage="uploading")
            if chunk == 0:
                self._crash("mid_upload")
        ctx.mark_request_sent()
        self._crash("after_marker")
        if self.fail_with is not None:
            if self.create_before_failing:
                self._create(ctx)
            raise self.fail_with
        external_id = self._create(ctx)
        self._crash("after_response")
        return SendResult(status="published", external_id=external_id)

    def _create(self, ctx: PublishContext) -> str:
        self.platform_state.creates.append(ctx.target_id)
        external_id = f"post-{ctx.target_id}-{len(self.platform_state.creates)}"
        self.platform_state.posts[ctx.target_id] = external_id
        return external_id

    def poll(self, ctx: PublishContext) -> PollResult:
        self.calls.append("poll")
        return PollResult(status="published")

    def verify(self, ctx: PublishContext) -> VerifyResult:
        self.calls.append("verify")
        found = self.platform_state.posts.get(ctx.target_id)
        return VerifyResult(verified=found is not None and found == ctx.external_id)

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        self.calls.append("reconcile")
        found = self.platform_state.posts.get(ctx.target_id)
        checked = ("the fake platform's posts",)
        if found is None:
            return ReconcileResult(status="absent", checked=checked)
        return ReconcileResult(status="published", external_id=found, checked=checked)


@dataclass
class Harness:
    database: Database
    settings: Settings
    clock: Clock
    fake: FakePublisher

    def registry(self) -> PublisherRegistry:
        registry = PublisherRegistry()
        registry.register(AssistedPublisher(self.database))
        registry.register(self.fake)
        return registry

    def engine(self) -> PublishingEngine:
        return PublishingEngine(self.database, self.settings, self.registry(), clock=self.clock)

    def service(self) -> PostService:
        return PostService(self.database, self.settings, self.registry(), clock=self.clock)

    def queue(self, engine: PublishingEngine) -> JobQueue:
        return JobQueue(
            self.database, clock=self.clock, expired_send_handler=engine.handle_expired_send
        )

    def worker(self) -> tuple[Worker, JobQueue]:
        engine = self.engine()
        queue = self.queue(engine)
        return Worker(queue=queue, handlers=engine.handlers(), worker_id="w"), queue

    def release(self, *, planned_in: timedelta = timedelta(0), caption: str = "Hi") -> int:
        with self.database.session() as session:
            account = ConnectedAccount(
                platform="youtube",
                external_account_id=f"UC-{len(self.fake.calls)}-{caption}",
                handle="@me",
                status="connected",
                granted_scopes_json="[]",
                metadata_json="{}",
            )
            session.add(account)
            session.flush()
            session.add(
                AccountPublisher(
                    account_id=account.id, post_type="youtube_post", publisher=self.fake.name
                )
            )
            account_id = account.id
        service = self.service()
        view = service.create_post(
            local_owner(),
            PostCreate(
                title="Post",
                body=caption,
                targets=[
                    TargetInput(
                        platform=Platform.youtube,
                        account_id=account_id,
                        post_type="youtube_post",
                        planned_at=self.clock.now + planned_in,
                    )
                ],
            ),
        )
        target = view.targets[0]
        assert target.publisher == self.fake.name, target.issues
        service.release_target(
            local_owner(),
            view.id,
            target.id,
            ReleaseRequest(payload_hash=target.payload_hash, confirmed=True),
        )
        return target.id

    def crash_once(self) -> None:
        """Claim the due send job and run it in this 'process' until it dies."""
        engine = self.engine()
        queue = self.queue(engine)
        job = queue.claim("doomed", engine.handlers().keys())
        assert job is not None and job.kind == "send"
        if self.fake.crash_at == "before_intent":
            self.fake.crash_at = None
            return
        with pytest.raises(SimulatedCrash):
            engine.run_send(job)

    def drain(self, rounds: int = 60) -> None:
        """Fresh workers until nothing is left, moving the clock to each due job."""
        for _ in range(rounds):
            worker, queue = self.worker()
            if worker.run_once():
                continue
            due = queue.next_run_at(worker.handlers.keys())
            if due is None:
                return
            self.clock.now = max(self.clock.now, due) + timedelta(seconds=1)
        raise AssertionError("the queue did not drain")

    def target(self, target_id: int) -> DraftTarget:
        with self.database.session() as session:
            row = session.get(DraftTarget, target_id)
            assert row is not None
            session.expunge(row)
            return row

    def attempts(self, target_id: int) -> list[tuple[str, str]]:
        with self.database.session() as session:
            return [
                (row.kind, row.outcome)
                for row in session.scalars(
                    select(TargetAttempt)
                    .where(TargetAttempt.target_id == target_id)
                    .order_by(TargetAttempt.id)
                )
            ]


@pytest.fixture()
def harness(database: Database, settings: Settings) -> Harness:
    return Harness(database, settings, Clock(), FakePublisher(FakePlatform()))


def test_a_clean_send_publishes_once_and_verifies_by_read_back(harness: Harness) -> None:
    target_id = harness.release()
    harness.drain()
    target = harness.target(target_id)
    assert target.state == "verified"
    assert harness.fake.platform_state.creates == [target_id]
    assert harness.attempts(target_id) == [("send", "published"), ("verify", "verified")]
    with harness.database.session() as session:
        attempt = session.scalar(select(TargetAttempt).where(TargetAttempt.kind == "send"))
        assert attempt is not None and attempt.request_sent_at is not None
        progress = session.scalars(
            select(EventRecord).where(EventRecord.type == "target.progress")
        ).all()
    # Three reports in one fast send: the first and the final one are written.
    assert 1 <= len(progress) <= 3


@pytest.mark.parametrize(
    ("crash_at", "final_state", "creates"),
    [
        ("before_intent", "verified", 1),
        ("after_intent", "verified", 1),
        ("mid_upload", "verified", 1),
        # The marker was committed but the creating call never left: reconcile
        # proves it is absent, and nothing is retried automatically.
        ("after_marker", "failed_retryable", 0),
        ("after_response", "verified", 1),
    ],
)
def test_killing_the_worker_at_every_step_never_duplicates_a_post(
    harness: Harness, crash_at: str, final_state: str, creates: int
) -> None:
    target_id = harness.release()
    harness.fake.crash_at = crash_at
    harness.crash_once()
    assert harness.fake.crash_at is None, "the crash point was reached"

    harness.drain()

    target = harness.target(target_id)
    assert target.state == final_state
    assert harness.fake.platform_state.creates == [target_id] * creates
    # The creating call is reached at most once; only an upload interrupted
    # before the marker is sent again (and resumes).
    assert harness.fake.calls.count("send") == (2 if crash_at == "mid_upload" else 1)
    if crash_at == "mid_upload":
        # The restarted worker resumed from the saved offset, not from zero.
        assert harness.fake.resumed_from[-1] == {"offset": 1}
        assert harness.fake.calls.count("chunk0") == 1
    if crash_at in {"after_marker", "after_response"}:
        assert "reconcile" in harness.fake.calls
        outcomes = harness.attempts(target_id)
        assert ("send", "uncertain") in outcomes
        assert ("reconcile", "absent" if crash_at == "after_marker" else "published") in outcomes
    with harness.database.session() as session:
        sends = session.scalars(
            select(PublishJob).where(PublishJob.kind == "send", PublishJob.state == "pending")
        ).all()
    assert sends == []


def test_a_read_timeout_on_the_final_call_is_uncertain_and_never_retried(
    harness: Harness,
) -> None:
    harness.fake.fail_with = httpx.ReadTimeout("no response")
    target_id = harness.release()
    worker, _ = harness.worker()
    assert worker.run_once()
    target = harness.target(target_id)
    assert target.state == "uncertain"
    with harness.database.session() as session:
        pending = [
            job.kind
            for job in session.scalars(select(PublishJob).where(PublishJob.state == "pending"))
        ]
    assert pending == ["reconcile"]

    harness.drain()

    assert harness.fake.calls.count("send") == 1
    # Nothing was created, so reconcile proves it absent; a person decides next.
    assert harness.target(target_id).state == "failed_retryable"
    assert "release again" in (harness.target(target_id).state_reason or "")


def test_a_timeout_after_the_post_was_created_reconciles_to_published(harness: Harness) -> None:
    harness.fake.fail_with = httpx.ReadTimeout("no response")
    harness.fake.create_before_failing = True
    target_id = harness.release()
    harness.drain()
    assert harness.fake.calls.count("send") == 1
    assert harness.fake.platform_state.creates == [target_id]
    assert harness.target(target_id).state == "verified"


def test_the_worker_refuses_a_payload_that_changed_after_release(harness: Harness) -> None:
    target_id = harness.release()
    with harness.database.session() as session:
        target = session.get(DraftTarget, target_id)
        assert target is not None
        # An edit that bypassed the service (e.g. a direct database change).
        target.caption_override = "something else"
    harness.drain()
    target = harness.target(target_id)
    assert target.state == "ready"
    assert target.release_state == "none"
    assert "send" not in harness.fake.calls
    assert harness.attempts(target_id) == [("send", "mismatch")]


def test_retries_are_bounded_to_three_after_1_5_and_20_minutes(harness: Harness) -> None:
    harness.fake.fail_with = PublishRetryLater("server_busy", "try later")
    target_id = harness.release()
    run_times: list[datetime] = []
    for _ in range(10):
        worker, queue = harness.worker()
        if worker.run_once():
            run_times.append(harness.clock.now)
            continue
        due = queue.next_run_at(worker.handlers.keys())
        if due is None:
            break
        harness.clock.now = due
    assert harness.fake.calls.count("send") == 4
    gaps = [later - earlier for earlier, later in zip(run_times, run_times[1:], strict=False)]
    assert gaps == [timedelta(minutes=1), timedelta(minutes=5), timedelta(minutes=20)]
    target = harness.target(target_id)
    assert target.state == "failed_retryable"
    assert "used up" in (target.state_reason or "")


def test_no_automatic_retry_past_planned_at_plus_six_hours(harness: Harness) -> None:
    harness.fake.fail_with = PublishRetryLater(
        "quota_exceeded", "daily quota used", retry_after=START + timedelta(hours=12)
    )
    target_id = harness.release()
    harness.drain()
    assert harness.fake.calls.count("send") == 1
    target = harness.target(target_id)
    assert target.state == "failed_retryable"
    assert "too late for an automatic retry" in (target.state_reason or "")


def test_a_target_more_than_an_hour_late_waits_for_a_person(harness: Harness) -> None:
    target_id = harness.release(planned_in=timedelta(minutes=30))
    harness.clock.advance(timedelta(minutes=30 + 61))
    harness.drain()
    target = harness.target(target_id)
    assert target.state == "ready"
    assert target.state_reason is not None and "missed while offline" in target.state_reason
    assert "send" not in harness.fake.calls


def test_a_target_less_than_an_hour_late_is_still_sent(harness: Harness) -> None:
    target_id = harness.release(planned_in=timedelta(minutes=30))
    harness.clock.advance(timedelta(minutes=30 + 59))
    harness.drain()
    assert harness.target(target_id).state == "verified"


def test_publishing_paused_holds_every_send(harness: Harness) -> None:
    target_id = harness.release()
    with harness.database.session() as session:
        session.add(PublishingControl(workspace_id=1, paused=True, reason="holiday"))
    worker, _ = harness.worker()
    assert worker.run_once()
    assert "send" not in harness.fake.calls
    assert harness.target(target_id).state == "queued"
    with harness.database.session() as session:
        job = session.scalar(select(PublishJob).where(PublishJob.kind == "send"))
        assert job is not None
        assert (job.state, job.attempts) == ("pending", 0)
        assert job.run_at.replace(tzinfo=UTC) > harness.clock.now
        control = session.get(PublishingControl, 1)
        assert control is not None
        control.paused = False
    harness.drain()
    assert harness.target(target_id).state == "verified"


def test_global_publishing_paused_setting(database: Database, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = Settings(data_dir=tmp_path / "data", publishing_paused=True)
    harness = Harness(database, settings, Clock(), FakePublisher(FakePlatform()))
    target_id = harness.release()
    worker, _ = harness.worker()
    assert worker.run_once()
    assert "send" not in harness.fake.calls
    assert harness.target(target_id).state == "queued"


def test_the_operator_workspace_kill_switch_holds_every_send(harness: Harness) -> None:
    # `runway hosted pause-publishing` (runway.hosted.quotas) stops the engine too.
    from runway.db.hosted_models import WorkspaceLimits

    target_id = harness.release()
    with harness.database.session() as session:
        session.add(
            WorkspaceLimits(
                workspace_id=1, publishing_disabled=True, publishing_disabled_reason="abuse"
            )
        )
    worker, _ = harness.worker()
    assert worker.run_once()
    assert "send" not in harness.fake.calls
    assert harness.target(target_id).state == "queued"


def test_a_preparing_adapter_starts_up_to_its_lead_before_planned_at(harness: Harness) -> None:
    harness.fake.prepare_lead = timedelta(minutes=30)
    target_id = harness.release(planned_in=timedelta(hours=2))
    with harness.database.session() as session:
        job = session.scalar(select(PublishJob).where(PublishJob.kind == "send"))
        assert job is not None
        assert job.run_at.replace(tzinfo=UTC) == START + timedelta(minutes=90)
    harness.drain()
    assert harness.fake.calls.index("prepare") < harness.fake.calls.index("send")
    assert harness.fake.resumed_from[0] == {"container": f"c-{target_id}"}
    assert harness.target(target_id).state == "verified"
    with harness.database.session() as session:
        sent = session.scalar(select(TargetAttempt).where(TargetAttempt.kind == "send"))
        assert sent is not None and sent.request_sent_at is not None
        assert sent.request_sent_at.replace(tzinfo=UTC) >= START + timedelta(hours=2)


def test_a_worker_that_dies_repeatedly_before_sending_stops_rerunning(harness: Harness) -> None:
    target_id = harness.release()
    for _ in range(3):
        harness.fake.crash_at = "after_intent"
        engine = harness.engine()
        queue = harness.queue(engine)
        harness.clock.advance(timedelta(minutes=6))
        job = queue.claim("doomed", engine.handlers().keys())
        assert job is not None
        with pytest.raises(SimulatedCrash):
            engine.run_send(job)
    harness.clock.advance(timedelta(minutes=6))
    worker, _ = harness.worker()
    worker.run_once()
    assert harness.target(target_id).state == "failed_retryable"
    assert harness.fake.platform_state.creates == []


# ------------------------------------------------------------- mock publishers


@pytest.fixture()
def mock_settings(tmp_path) -> Settings:  # type: ignore[no-untyped-def]
    return Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        mock_publish_processing_seconds=5,
    )


@pytest.mark.parametrize(
    ("scenario", "final_state"),
    [
        ("success", "verified"),
        ("uncertain", "verified"),
        ("container_error", "failed_final"),
        ("processing_failure", "failed_final"),
        ("revoked_token", "failed_final"),
        ("quota_exceeded", "failed_retryable"),
    ],
)
def test_mock_publishers_walk_the_whole_machine(
    database: Database, mock_settings: Settings, scenario: str, final_state: str
) -> None:
    clock = Clock()
    registry = build_publisher_registry(mock_settings, database, clock=clock)
    service = PostService(database, mock_settings, registry, clock=clock)
    with database.session() as session:
        account = ConnectedAccount(
            platform="youtube",
            external_account_id="UC-mock",
            status="connected",
            granted_scopes_json="[]",
            metadata_json="{}",
        )
        session.add(account)
        session.flush()
        account_id = account.id
    view = service.create_post(
        local_owner(),
        PostCreate(
            title="Mock",
            body=f"Hello [mock:{scenario}]",
            targets=[
                TargetInput(
                    platform=Platform.youtube,
                    account_id=account_id,
                    post_type="youtube_post",
                    planned_at=clock.now,
                )
            ],
        ),
    )
    target = view.targets[0]
    assert target.publisher == "youtube_post"
    service.release_target(
        local_owner(),
        view.id,
        target.id,
        ReleaseRequest(payload_hash=target.payload_hash, confirmed=True),
    )
    engine = PublishingEngine(database, mock_settings, registry, clock=clock)
    queue = JobQueue(database, clock=clock, expired_send_handler=engine.handle_expired_send)
    seen: list[str] = []
    for _ in range(60):
        worker = Worker(queue=queue, handlers=engine.handlers(), worker_id="w")
        if not worker.run_once():
            due = queue.next_run_at(engine.handlers().keys())
            if due is None:
                break
            clock.now = max(clock.now, due) + timedelta(seconds=1)
        with database.session() as session:
            state = session.scalar(select(DraftTarget.state).where(DraftTarget.id == target.id))
        if not seen or seen[-1] != state:
            seen.append(str(state))
    assert seen[-1] == final_state, seen
    if scenario == "success":
        assert seen == ["sending", "processing", "published", "verified"] or seen[-3:] == [
            "processing",
            "published",
            "verified",
        ]
    with database.session() as session:
        changes = [
            json.loads(event.payload_json)["to"]
            for event in session.scalars(
                select(EventRecord)
                .where(EventRecord.type == "target.state_changed")
                .order_by(EventRecord.id)
            )
        ]
    assert changes[:3] == ["ready", "released", "queued"]
    assert changes[-1] == final_state
