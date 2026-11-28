from __future__ import annotations

import asyncio
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import IntelligenceAgentRun, IntelligenceAgentStep
from runway.db.repositories import get_channel
from runway.intelligence.agent_harness import (
    AdaptiveEditorialRouter,
    AgentBudget,
    AgentBudgetExceeded,
    AgentHarnessError,
    AgentInputEnvelope,
    AgentOutputEnvelope,
    AgentPipelineStep,
    AgentStepResult,
    CapabilitySpec,
    IntelligenceAgentHarness,
    IntelligenceCapabilityRegistry,
)
from runway.intelligence.embeddings import configuration_hash


@pytest.mark.asyncio
async def test_agent_harness_persists_typed_success_and_idempotent_replay(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    harness = IntelligenceAgentHarness(database)
    calls = 0

    async def handler(value: AgentInputEnvelope, attempt: int) -> AgentStepResult:
        nonlocal calls
        calls += 1
        return AgentStepResult(
            output=AgentOutputEnvelope(
                payload={
                    "received": value.payload["caption"],
                    "attempt": attempt,
                }
            ),
            usage={"prompt_tokens": 3, "completion_tokens": 2},
            artifact_type="caption_slate",
            artifact_id="fixture-1",
        )

    run_id, output = await harness.execute(
        channel_id=channel_id,
        capability="caption_generation",
        provider="mock",
        model="fixture",
        prompt_version="fixture-v1",
        input_value=AgentInputEnvelope(
            entity_ids=[1],
            payload={"caption": "Why so excited?"},
        ),
        handler=handler,
        run_key="fixture-success",
    )
    repeated_id, repeated_output = await harness.execute(
        channel_id=channel_id,
        capability="caption_generation",
        provider="mock",
        model="fixture",
        prompt_version="fixture-v1",
        input_value=AgentInputEnvelope(
            entity_ids=[1],
            payload={"caption": "Why so excited?"},
        ),
        handler=handler,
        run_key="fixture-success",
    )
    assert repeated_id == run_id
    assert repeated_output == output
    assert calls == 1
    detail = harness.inspect(run_id)
    assert detail["status"] == "completed"
    assert detail["usage"] == {
        "completion_tokens": 2,
        "prompt_tokens": 3,
        "total_tokens": 5,
    }
    assert detail["steps"][0]["artifact_type"] == "caption_slate"  # type: ignore[index]


@pytest.mark.asyncio
async def test_agent_harness_retries_persists_failures_and_enforces_budget(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    harness = IntelligenceAgentHarness(database)

    async def flaky(_value: AgentInputEnvelope, attempt: int) -> AgentStepResult:
        if attempt == 1:
            raise RuntimeError("fixture failure")
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"ok": True}),
            usage={"total_tokens": 4},
        )

    run_id, _output = await harness.execute(
        channel_id=channel_id,
        capability="candidate_analysis",
        provider="mock",
        model="fixture",
        prompt_version="fixture-v1",
        input_value=AgentInputEnvelope(payload={"candidate_id": 1}),
        handler=flaky,
        budget=AgentBudget(
            max_steps=2,
            max_attempts_per_step=2,
            max_total_tokens=10,
            max_seconds=10,
            timeout_seconds=2,
        ),
    )
    detail = harness.inspect(run_id)
    assert [step["status"] for step in detail["steps"]] == ["failed", "completed"]  # type: ignore[index]

    async def expensive(
        _value: AgentInputEnvelope,
        _attempt: int,
    ) -> AgentStepResult:
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"ok": True}),
            usage={"total_tokens": 11},
        )

    with pytest.raises(AgentBudgetExceeded, match="token budget"):
        await harness.execute(
            channel_id=channel_id,
            capability="candidate_analysis",
            provider="mock",
            model="fixture",
            prompt_version="fixture-v1",
            input_value=AgentInputEnvelope(payload={"candidate_id": 2}),
            handler=expensive,
            budget=AgentBudget(
                max_steps=1,
                max_attempts_per_step=1,
                max_total_tokens=10,
                max_seconds=10,
                timeout_seconds=2,
            ),
        )
    with database.session() as session:
        failed = session.scalar(
            select(IntelligenceAgentRun)
            .where(IntelligenceAgentRun.status == "failed")
            .order_by(IntelligenceAgentRun.id.desc())
            .limit(1)
        )
        assert failed is not None
        assert "token budget" in str(failed.error_summary)
        assert (
            session.scalar(
                select(func.count(IntelligenceAgentStep.id)).where(
                    IntelligenceAgentStep.agent_run_id == failed.id
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_agent_harness_timeout_and_capability_registry_fail_closed(
    database: Database,
    settings: Settings,
) -> None:
    registry = IntelligenceCapabilityRegistry()
    assert registry.status()["publishing_capability_present"] is False
    with pytest.raises(ValueError, match="publishing capabilities"):
        registry.register(
            CapabilitySpec(
                name="publish_youtube",
                input_type=AgentInputEnvelope,
                output_type=AgentOutputEnvelope,
                description="forbidden",
            )
        )
    with pytest.raises(LookupError, match="no fallback"):
        registry.resolve("unknown")

    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    harness = IntelligenceAgentHarness(database, registry)

    async def slow(
        _value: AgentInputEnvelope,
        _attempt: int,
    ) -> AgentStepResult:
        await asyncio.sleep(0.05)
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"late": True}),
            usage={},
        )

    budget = AgentBudget(
        max_steps=1,
        max_attempts_per_step=1,
        max_total_tokens=10,
        max_seconds=1,
        timeout_seconds=0.01,
    )
    with pytest.raises(AgentHarnessError, match="failed after"):
        await harness.execute(
            channel_id=channel_id,
            capability="caption_verification",
            provider="mock",
            model="fixture",
            prompt_version="fixture-v1",
            input_value=AgentInputEnvelope(payload={"caption": "fixture"}),
            handler=slow,
            budget=budget,
            run_key="fixture-timeout-retry",
        )

    async def fast(_value: AgentInputEnvelope, _attempt: int) -> AgentStepResult:
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"late": False}),
            usage={"total_tokens": 1},
        )

    with database.session() as session:
        timed_out = session.scalar(
            select(IntelligenceAgentRun).where(
                IntelligenceAgentRun.run_key == "fixture-timeout-retry"
            )
        )
        assert timed_out is not None
        assert timed_out.status == "failed"
        timed_out_id = timed_out.id
    retry_id, retry_output = await harness.execute(
        channel_id=channel_id,
        capability="caption_verification",
        provider="mock",
        model="fixture",
        prompt_version="fixture-v1",
        input_value=AgentInputEnvelope(payload={"caption": "fixture"}),
        handler=fast,
        budget=budget,
        run_key="fixture-timeout-retry",
    )
    assert retry_id == timed_out_id
    assert retry_output.payload == {"late": False}


@pytest.mark.asyncio
async def test_agent_harness_persists_typed_abstention_without_retry(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    harness = IntelligenceAgentHarness(database)
    calls = 0

    async def abstain(
        _value: AgentInputEnvelope,
        _attempt: int,
    ) -> AgentStepResult:
        nonlocal calls
        calls += 1
        return AgentStepResult(
            output=AgentOutputEnvelope(
                payload={
                    "abstained": True,
                    "reason_code": "insufficient_visible_evidence",
                }
            ),
            usage={"total_tokens": 2},
            abstention_reason="Visible evidence is insufficient for a grounded caption.",
        )

    run_id, output = await harness.execute(
        channel_id=channel_id,
        capability="caption_verification",
        provider="mock",
        model="fixture",
        prompt_version="fixture-v1",
        input_value=AgentInputEnvelope(payload={"caption": "What happens next?"}),
        handler=abstain,
        budget=AgentBudget(max_attempts_per_step=3),
        run_key="fixture-abstention",
    )

    assert output.payload["abstained"] is True
    assert calls == 1
    detail = harness.inspect(run_id)
    assert detail["status"] == "abstained"
    assert detail["error_summary"] == ("Visible evidence is insufficient for a grounded caption.")
    assert detail["steps"][0]["status"] == "abstained"  # type: ignore[index]

    repeated_id, repeated_output = await harness.execute(
        channel_id=channel_id,
        capability="caption_verification",
        provider="mock",
        model="fixture",
        prompt_version="fixture-v1",
        input_value=AgentInputEnvelope(payload={"caption": "What happens next?"}),
        handler=abstain,
        budget=AgentBudget(max_attempts_per_step=3),
        run_key="fixture-abstention",
    )
    assert repeated_id == run_id
    assert repeated_output == output
    assert calls == 1


def test_agent_harness_persists_one_adaptive_observed_parent_trace(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    harness = IntelligenceAgentHarness(database)
    input_value = AgentInputEnvelope(
        entity_ids=[17],
        payload={"candidate_id": 17, "caption_slate_id": 23},
    )
    plan = AdaptiveEditorialRouter.plan(
        input_value=input_value,
        image_fact_confidence=0.92,
        retrieval_coverage=0.9,
        rank_separation=0.2,
        factual_claim_risk=False,
        identity_conflict=False,
    )
    outputs = {
        step.capability: AgentOutputEnvelope(
            payload={"observed": True, "capability": step.capability}
        )
        for step in plan.steps
    }

    run_id = harness.persist_observed_pipeline(
        channel_id=channel_id,
        plan=plan,
        outputs=outputs,
        provider="mock",
        model="fixture",
        prompt_version="captions-v5",
        run_key="observed-caption-slate-23",
        terminal_status="completed",
        artifact_type="caption_slate",
        artifact_id=23,
        usage={"prompt_tokens": 4, "completion_tokens": 3},
    )
    replayed_id = harness.persist_observed_pipeline(
        channel_id=channel_id,
        plan=plan,
        outputs=outputs,
        provider="mock",
        model="fixture",
        prompt_version="captions-v5",
        run_key="observed-caption-slate-23",
        terminal_status="completed",
        artifact_type="caption_slate",
        artifact_id=23,
        usage={"prompt_tokens": 4, "completion_tokens": 3},
    )

    assert replayed_id == run_id
    detail = harness.inspect(run_id)
    assert detail["capability"] == "editorial_pipeline"
    assert detail["input"]["execution_mode"] == "observed_canonical_trace"  # type: ignore[index]
    assert [step["capability"] for step in detail["steps"]] == [  # type: ignore[index]
        step.capability for step in plan.steps
    ]
    assert detail["steps"][-1]["artifact_type"] == "caption_slate"  # type: ignore[index]
    assert detail["steps"][-1]["artifact_id"] == "23"  # type: ignore[index]


@pytest.mark.asyncio
async def test_failed_deterministic_run_key_retries_without_duplicating_the_logical_run(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    harness = IntelligenceAgentHarness(database)
    input_value = AgentInputEnvelope(payload={"candidate_id": 7})
    budget = AgentBudget(
        max_steps=1,
        max_attempts_per_step=1,
        max_total_tokens=20,
        max_seconds=5,
        timeout_seconds=1,
    )

    async def fail_once(_value: AgentInputEnvelope, _attempt: int) -> AgentStepResult:
        raise RuntimeError("transient fixture failure")

    with pytest.raises(AgentHarnessError, match="failed after"):
        await harness.execute(
            channel_id=channel_id,
            capability="candidate_analysis",
            provider="mock",
            model="fixture",
            prompt_version="fixture-v1",
            input_value=input_value,
            handler=fail_once,
            budget=budget,
            run_key="fixture-retryable-failure",
        )
    with database.session() as session:
        failed = session.scalar(
            select(IntelligenceAgentRun).where(
                IntelligenceAgentRun.run_key == "fixture-retryable-failure"
            )
        )
        assert failed is not None
        assert failed.status == "failed"
        failed_id = failed.id

    retry_attempts: list[int] = []

    async def succeeds_on_retry(_value: AgentInputEnvelope, attempt: int) -> AgentStepResult:
        retry_attempts.append(attempt)
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"recovered": True}),
            usage={"total_tokens": 1},
        )

    retry_id, output = await harness.execute(
        channel_id=channel_id,
        capability="candidate_analysis",
        provider="mock",
        model="fixture",
        prompt_version="fixture-v1",
        input_value=input_value,
        handler=succeeds_on_retry,
        budget=budget,
        run_key="fixture-retryable-failure",
    )

    assert retry_id == failed_id
    assert output.payload == {"recovered": True}
    assert retry_attempts == [2]
    detail = harness.inspect(failed_id)
    assert detail["status"] == "completed"
    assert detail["attempt_count"] == 2
    assert [
        (step["attempt"], step["status"])
        for step in detail["steps"]  # type: ignore[index]
    ] == [(1, "failed"), (2, "completed")]

    replay_id, replay_output = await harness.execute(
        channel_id=channel_id,
        capability="candidate_analysis",
        provider="mock",
        model="fixture",
        prompt_version="fixture-v1",
        input_value=input_value,
        handler=succeeds_on_retry,
        budget=budget,
        run_key="fixture-retryable-failure",
    )
    assert replay_id == failed_id
    assert replay_output == output
    assert retry_attempts == [2]

    with pytest.raises(AgentHarnessError, match="different logical request"):
        await harness.execute(
            channel_id=channel_id,
            capability="candidate_analysis",
            provider="mock",
            model="fixture",
            prompt_version="fixture-v1",
            input_value=AgentInputEnvelope(payload={"candidate_id": 8}),
            handler=succeeds_on_retry,
            budget=budget,
            run_key="fixture-retryable-failure",
        )
    with pytest.raises(AgentHarnessError, match="different logical request"):
        await harness.execute(
            channel_id=channel_id,
            capability="candidate_analysis",
            provider="different-provider",
            model="fixture",
            prompt_version="fixture-v1",
            input_value=input_value,
            handler=succeeds_on_retry,
            budget=budget,
            run_key="fixture-retryable-failure",
        )
    with database.session() as session:
        assert (
            session.scalar(
                select(func.count(IntelligenceAgentRun.id)).where(
                    IntelligenceAgentRun.run_key == "fixture-retryable-failure"
                )
            )
            == 1
        )


@pytest.mark.asyncio
async def test_cancelled_run_is_recovered_only_after_its_lease_expires(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    harness = IntelligenceAgentHarness(database, lease_seconds=0.2)
    started = asyncio.Event()

    async def stuck(_value: AgentInputEnvelope, _attempt: int) -> AgentStepResult:
        started.set()
        await asyncio.sleep(60)
        raise AssertionError("the cancelled fixture unexpectedly completed")

    input_value = AgentInputEnvelope(payload={"candidate_id": 11})
    task = asyncio.create_task(
        harness.execute(
            channel_id=channel_id,
            capability="candidate_analysis",
            provider="mock",
            model="fixture",
            prompt_version="fixture-v1",
            input_value=input_value,
            handler=stuck,
            run_key="fixture-crash-stale-running",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    with database.session() as session:
        crashed = session.scalar(
            select(IntelligenceAgentRun).where(
                IntelligenceAgentRun.run_key == "fixture-crash-stale-running"
            )
        )
        assert crashed is not None
        assert crashed.status == "running"
        crashed_id = crashed.id
        crashed.lease_expires_at = datetime.now(UTC) - timedelta(seconds=0.001)

    retry_attempts: list[int] = []

    async def recovered(_value: AgentInputEnvelope, attempt: int) -> AgentStepResult:
        retry_attempts.append(attempt)
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"recovered": True}),
            usage={"total_tokens": 0},
        )

    recovered_id, _output = await harness.execute(
        channel_id=channel_id,
        capability="candidate_analysis",
        provider="mock",
        model="fixture",
        prompt_version="fixture-v1",
        input_value=input_value,
        handler=recovered,
        run_key="fixture-crash-stale-running",
    )

    assert recovered_id == crashed_id
    assert retry_attempts == [2]
    detail = harness.inspect(crashed_id)
    assert detail["status"] == "completed"
    assert [
        (step["attempt"], step["status"])
        for step in detail["steps"]  # type: ignore[index]
    ] == [(1, "failed"), (2, "completed")]
    assert "stale lease" in str(detail["steps"][0]["error_summary"])  # type: ignore[index]


def _seed_running_run(
    database: Database,
    *,
    channel_id: int,
    run_key: str,
    lease_expires_at: datetime | None,
) -> int:
    input_value = AgentInputEnvelope(payload={"candidate_id": 1})
    budget = AgentBudget()
    configuration = {
        "capability": "candidate_analysis",
        "provider": "mock",
        "model": "fixture",
        "prompt_version": "fixture-v1",
        "budget": budget.model_dump(),
    }
    with database.session() as session:
        run = IntelligenceAgentRun(
            channel_id=channel_id,
            run_key=run_key,
            capability="candidate_analysis",
            provider="mock",
            model="fixture",
            prompt_version="fixture-v1",
            status="running",
            input_json=input_value.model_dump_json(),
            output_json="{}",
            budget_json=budget.model_dump_json(),
            usage_json="{}",
            configuration_hash=configuration_hash(configuration),
            attempt_count=0,
            lease_owner="crashed-owner",
            lease_expires_at=lease_expires_at,
        )
        session.add(run)
        session.flush()
        return run.id


@pytest.mark.asyncio
async def test_active_run_heartbeat_protects_lease_past_initial_expiry(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    harness = IntelligenceAgentHarness(database, lease_seconds=0.1)
    started = asyncio.Event()
    release = asyncio.Event()

    async def active(_value: AgentInputEnvelope, _attempt: int) -> AgentStepResult:
        started.set()
        await release.wait()
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"finished": True}),
            usage={"total_tokens": 0},
        )

    input_value = AgentInputEnvelope(payload={"candidate_id": 12})
    first = asyncio.create_task(
        harness.execute(
            channel_id=channel_id,
            capability="candidate_analysis",
            provider="mock",
            model="fixture",
            prompt_version="fixture-v1",
            input_value=input_value,
            handler=active,
            run_key="fixture-heartbeat-active",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(0.15)
    with pytest.raises(AgentHarnessError, match="already claimed"):
        await harness.execute(
            channel_id=channel_id,
            capability="candidate_analysis",
            provider="mock",
            model="fixture",
            prompt_version="fixture-v1",
            input_value=input_value,
            handler=active,
            run_key="fixture-heartbeat-active",
        )
    release.set()
    run_id, output = await first
    assert output.payload == {"finished": True}
    assert harness.inspect(run_id)["lease_owner"] is None


@pytest.mark.asyncio
async def test_lease_expiry_clock_boundary_is_explicit(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    fixed_now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    harness = IntelligenceAgentHarness(database, clock=lambda: fixed_now, lease_seconds=5)
    input_value = AgentInputEnvelope(payload={"candidate_id": 1})

    async def success(_value: AgentInputEnvelope, _attempt: int) -> AgentStepResult:
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"ok": True}),
            usage={"total_tokens": 0},
        )

    expired_id = _seed_running_run(
        database,
        channel_id=channel_id,
        run_key="fixture-lease-expired-at-now",
        lease_expires_at=fixed_now,
    )
    recovered_id, _output = await harness.execute(
        channel_id=channel_id,
        capability="candidate_analysis",
        provider="mock",
        model="fixture",
        prompt_version="fixture-v1",
        input_value=input_value,
        handler=success,
        run_key="fixture-lease-expired-at-now",
    )
    assert recovered_id == expired_id

    legacy_id = _seed_running_run(
        database,
        channel_id=channel_id,
        run_key="fixture-lease-legacy-absent",
        lease_expires_at=None,
    )
    legacy_recovered_id, _legacy_output = await harness.execute(
        channel_id=channel_id,
        capability="candidate_analysis",
        provider="mock",
        model="fixture",
        prompt_version="fixture-v1",
        input_value=input_value,
        handler=success,
        run_key="fixture-lease-legacy-absent",
    )
    assert legacy_recovered_id == legacy_id

    active_id = _seed_running_run(
        database,
        channel_id=channel_id,
        run_key="fixture-lease-still-active",
        lease_expires_at=fixed_now + timedelta(seconds=1),
    )
    with pytest.raises(AgentHarnessError, match="already claimed"):
        await harness.execute(
            channel_id=channel_id,
            capability="candidate_analysis",
            provider="mock",
            model="fixture",
            prompt_version="fixture-v1",
            input_value=input_value,
            handler=success,
            run_key="fixture-lease-still-active",
        )
    with database.session() as session:
        active = session.get(IntelligenceAgentRun, active_id)
        assert active is not None
        assert active.status == "running"
        assert active.lease_owner == "crashed-owner"


@pytest.mark.asyncio
async def test_out_of_order_zombie_completion_cannot_overwrite_recovered_run(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    original_now = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    original = IntelligenceAgentHarness(
        database,
        clock=lambda: original_now,
        lease_seconds=5,
    )
    recovery = IntelligenceAgentHarness(
        database,
        clock=lambda: original_now + timedelta(seconds=10),
        lease_seconds=5,
    )
    input_value = AgentInputEnvelope(payload={"candidate_id": 22})
    started = asyncio.Event()
    release = asyncio.Event()

    async def zombie(_value: AgentInputEnvelope, _attempt: int) -> AgentStepResult:
        started.set()
        await release.wait()
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"winner": "zombie"}),
            usage={"total_tokens": 0},
        )

    async def recovered(_value: AgentInputEnvelope, _attempt: int) -> AgentStepResult:
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"winner": "retry"}),
            usage={"total_tokens": 0},
        )

    zombie_task = asyncio.create_task(
        original.execute(
            channel_id=channel_id,
            capability="candidate_analysis",
            provider="mock",
            model="fixture",
            prompt_version="fixture-v1",
            input_value=input_value,
            handler=zombie,
            run_key="fixture-out-of-order-completion",
        )
    )
    await asyncio.wait_for(started.wait(), timeout=1)

    recovered_id, recovered_output = await recovery.execute(
        channel_id=channel_id,
        capability="candidate_analysis",
        provider="mock",
        model="fixture",
        prompt_version="fixture-v1",
        input_value=input_value,
        handler=recovered,
        run_key="fixture-out-of-order-completion",
    )
    assert recovered_output.payload == {"winner": "retry"}

    release.set()
    with pytest.raises(AgentHarnessError, match="lease ownership was lost"):
        await zombie_task

    inspection = recovery.inspect(recovered_id)
    assert inspection["status"] == "completed"
    assert inspection["output"] == {"payload": {"winner": "retry"}}
    assert [
        (step["sequence"], step["attempt"], step["status"]) for step in inspection["steps"]
    ] == [(1, 1, "failed"), (1, 2, "completed")]


def test_concurrent_same_key_creation_has_one_controlled_winner(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    del database  # each worker thread uses its own engine against the same database file
    barrier = threading.Barrier(2)
    release = threading.Event()
    outcomes: list[tuple[str, object]] = []
    input_value = AgentInputEnvelope(payload={"candidate_id": 21})

    class SynchronizedHarness(IntelligenceAgentHarness):
        def __init__(self, database: Database) -> None:
            super().__init__(database, lease_seconds=1)
            self._synchronize_next_claim = True

        def _lease_expiry(self) -> datetime:
            if self._synchronize_next_claim:
                self._synchronize_next_claim = False
                barrier.wait(timeout=3)
            return super()._lease_expiry()

    def worker(worker_index: int) -> None:
        local_database = Database(settings)
        harness = SynchronizedHarness(local_database)

        async def handler(_value: AgentInputEnvelope, _attempt: int) -> AgentStepResult:
            await asyncio.to_thread(release.wait, 2)
            return AgentStepResult(
                output=AgentOutputEnvelope(payload={"worker": worker_index}),
                usage={"total_tokens": 0},
            )

        try:
            barrier.wait(timeout=3)
            result = asyncio.run(
                harness.execute(
                    channel_id=channel_id,
                    capability="candidate_analysis",
                    provider="mock",
                    model="fixture",
                    prompt_version="fixture-v1",
                    input_value=input_value,
                    handler=handler,
                    run_key="fixture-concurrent-same-key",
                )
            )
            outcomes.append(("completed", result))
        except Exception as exc:  # the thread boundary requires preserving the exception object
            outcomes.append(("error", exc))
        finally:
            local_database.engine.dispose()

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(2)]
    for thread in threads:
        thread.start()
    time.sleep(0.1)
    release.set()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    completed = [value for kind, value in outcomes if kind == "completed"]
    errors = [value for kind, value in outcomes if kind == "error"]
    assert len(completed) == 1
    assert len(errors) == 1
    error = errors[0]
    assert isinstance(error, AgentHarnessError)
    assert not isinstance(error, IntegrityError)
    assert "already claimed" in str(error)

    verification_database = Database(settings)
    try:
        with verification_database.session() as session:
            runs = session.scalars(
                select(IntelligenceAgentRun).where(
                    IntelligenceAgentRun.run_key == "fixture-concurrent-same-key"
                )
            ).all()
            assert len(runs) == 1
            assert runs[0].status == "completed"
    finally:
        verification_database.engine.dispose()

    replay_database = Database(settings)
    try:
        harness = IntelligenceAgentHarness(replay_database)

        async def unused(_value: AgentInputEnvelope, _attempt: int) -> AgentStepResult:
            raise AssertionError("a completed same-key run must replay without re-executing")

        replay_id, replay_output = asyncio.run(
            harness.execute(
                channel_id=channel_id,
                capability="candidate_analysis",
                provider="mock",
                model="fixture",
                prompt_version="fixture-v1",
                input_value=input_value,
                handler=unused,
                run_key="fixture-concurrent-same-key",
            )
        )
        assert replay_id == completed[0][0]  # type: ignore[index]
        assert "worker" in replay_output.payload
    finally:
        replay_database.engine.dispose()


@pytest.mark.asyncio
async def test_failed_pipeline_retry_preserves_partial_history_and_step_attempt_order(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    harness = IntelligenceAgentHarness(database)
    input_value = AgentInputEnvelope(payload={"candidate_id": 31})
    steps = [
        AgentPipelineStep(capability="caption_generation", input_value=input_value),
        AgentPipelineStep(capability="caption_verification", input_value=input_value),
    ]

    async def generate(_value: AgentInputEnvelope, _attempt: int) -> AgentStepResult:
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"caption": "unicode fixture ✓"}),
            usage={"total_tokens": 1},
        )

    verification_calls = 0

    async def verify(_value: AgentInputEnvelope, _attempt: int) -> AgentStepResult:
        nonlocal verification_calls
        verification_calls += 1
        if verification_calls == 1:
            raise RuntimeError("partial fixture failure")
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"verified": True}),
            usage={"total_tokens": 1},
        )

    budget = AgentBudget(
        max_steps=2,
        max_attempts_per_step=1,
        max_total_tokens=10,
        max_seconds=10,
        timeout_seconds=2,
    )
    handlers = {"caption_generation": generate, "caption_verification": verify}
    with pytest.raises(AgentHarnessError, match="required pipeline step"):
        await harness.execute_pipeline(
            channel_id=channel_id,
            steps=steps,
            handlers=handlers,
            provider="mock",
            model="fixture",
            prompt_version="fixture-v1",
            risk_route="easy",
            budget=budget,
            run_key="fixture-pipeline-partial-retry",
        )
    with database.session() as session:
        failed = session.scalar(
            select(IntelligenceAgentRun).where(
                IntelligenceAgentRun.run_key == "fixture-pipeline-partial-retry"
            )
        )
        assert failed is not None
        assert failed.status == "failed"
        failed_id = failed.id

    retry_id, outputs = await harness.execute_pipeline(
        channel_id=channel_id,
        steps=steps,
        handlers=handlers,
        provider="mock",
        model="fixture",
        prompt_version="fixture-v1",
        risk_route="easy",
        budget=budget,
        run_key="fixture-pipeline-partial-retry",
    )

    assert retry_id == failed_id
    assert outputs["caption_verification"].payload["verified"] is True
    detail = harness.inspect(failed_id)
    step_states = {
        (step["sequence"], step["attempt"]): step["status"]  # type: ignore[index]
        for step in detail["steps"]  # type: ignore[index]
    }
    assert step_states == {
        (1, 1): "completed",
        (1, 2): "completed",
        (2, 1): "failed",
        (2, 2): "completed",
    }
    assert detail["attempt_count"] == 4


@pytest.mark.asyncio
async def test_cancelled_pipeline_recovers_stale_running_step_with_monotonic_attempts(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    harness = IntelligenceAgentHarness(database, lease_seconds=0.2)
    input_value = AgentInputEnvelope(payload={"candidate_id": 32})
    steps = [
        AgentPipelineStep(capability="caption_generation", input_value=input_value),
        AgentPipelineStep(capability="caption_verification", input_value=input_value),
    ]
    started = asyncio.Event()
    verification_calls = 0

    async def generate(_value: AgentInputEnvelope, _attempt: int) -> AgentStepResult:
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"caption": "pipeline recovery fixture"}),
            usage={"total_tokens": 1},
        )

    async def verify(_value: AgentInputEnvelope, _attempt: int) -> AgentStepResult:
        nonlocal verification_calls
        verification_calls += 1
        if verification_calls == 1:
            started.set()
            await asyncio.sleep(60)
            raise AssertionError("the cancelled pipeline fixture unexpectedly completed")
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"verified": True}),
            usage={"total_tokens": 1},
        )

    kwargs = {
        "channel_id": channel_id,
        "steps": steps,
        "handlers": {"caption_generation": generate, "caption_verification": verify},
        "provider": "mock",
        "model": "fixture",
        "prompt_version": "fixture-v1",
        "risk_route": "easy",
        "budget": AgentBudget(
            max_steps=2,
            max_attempts_per_step=1,
            max_total_tokens=10,
            max_seconds=10,
            timeout_seconds=2,
        ),
        "run_key": "fixture-pipeline-stale-recovery",
    }
    interrupted = asyncio.create_task(harness.execute_pipeline(**kwargs))
    await asyncio.wait_for(started.wait(), timeout=1)
    interrupted.cancel()
    with pytest.raises(asyncio.CancelledError):
        await interrupted

    with database.session() as session:
        crashed = session.scalar(
            select(IntelligenceAgentRun).where(
                IntelligenceAgentRun.run_key == "fixture-pipeline-stale-recovery"
            )
        )
        assert crashed is not None
        assert crashed.status == "running"
        crashed_id = crashed.id
        crashed.lease_expires_at = datetime.now(UTC) - timedelta(seconds=0.001)

    recovered_id, outputs = await harness.execute_pipeline(**kwargs)
    assert recovered_id == crashed_id
    assert outputs["caption_verification"].payload["verified"] is True
    detail = harness.inspect(crashed_id)
    step_states = {
        (step["sequence"], step["attempt"]): step["status"]  # type: ignore[index]
        for step in detail["steps"]  # type: ignore[index]
    }
    assert step_states == {
        (1, 1): "completed",
        (1, 2): "completed",
        (2, 1): "failed",
        (2, 2): "completed",
    }
    stale_steps = [
        step
        for step in detail["steps"]  # type: ignore[index]
        if (step["sequence"], step["attempt"]) == (2, 1)
    ]
    assert "stale lease" in str(stale_steps[0]["error_summary"])
    assert detail["attempt_count"] == 4


@pytest.mark.asyncio
async def test_edge_payloads_replay_by_exact_logical_input_and_distinct_keys_can_overlap(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    harness = IntelligenceAgentHarness(database)
    edge_payload = {
        "empty": "",
        "none": None,
        "zero": 0,
        "negative": -1,
        "huge": "x" * 5000,
        "unicode": "åß∂ƒ©˙∆˚¬…æ🔥",
        "nested": {"out_of_order": [3, 1, 2], "false": False},
    }
    calls = 0

    async def handler(value: AgentInputEnvelope, _attempt: int) -> AgentStepResult:
        nonlocal calls
        calls += 1
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"echo": value.payload}),
            usage={"total_tokens": 0},
        )

    kwargs = {
        "channel_id": channel_id,
        "capability": "candidate_analysis",
        "provider": "mock",
        "model": "fixture",
        "prompt_version": "fixture-v1",
        "input_value": AgentInputEnvelope(payload=edge_payload),
        "handler": handler,
    }
    first_id, first_output = await harness.execute(
        **kwargs,
        run_key="fixture-edge-payloads",
    )
    replay_id, replay_output = await harness.execute(
        **kwargs,
        run_key="fixture-edge-payloads",
    )
    assert replay_id == first_id
    assert replay_output == first_output
    assert replay_output.payload["echo"] == edge_payload  # type: ignore[index]
    assert calls == 1

    gate = asyncio.Event()

    async def gated(value: AgentInputEnvelope, _attempt: int) -> AgentStepResult:
        await gate.wait()
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"key": value.payload["key"]}),
            usage={"total_tokens": 0},
        )

    async def distinct(key: str):
        return await harness.execute(
            channel_id=channel_id,
            capability="candidate_analysis",
            provider="mock",
            model="fixture",
            prompt_version="fixture-v1",
            input_value=AgentInputEnvelope(payload={"key": key}),
            handler=gated,
            run_key=f"fixture-distinct-{key}",
        )

    first_task = asyncio.create_task(distinct("a"))
    second_task = asyncio.create_task(distinct("b"))
    await asyncio.sleep(0)
    gate.set()
    (
        (first_distinct_id, first_distinct_output),
        (second_distinct_id, second_distinct_output),
    ) = await asyncio.gather(first_task, second_task)
    assert first_distinct_id != second_distinct_id
    assert first_distinct_output.payload["key"] == "a"
    assert second_distinct_output.payload["key"] == "b"


@pytest.mark.asyncio
async def test_logical_input_identity_distinguishes_boolean_from_integer(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    harness = IntelligenceAgentHarness(database)
    calls = 0

    async def handler(value: AgentInputEnvelope, _attempt: int) -> AgentStepResult:
        nonlocal calls
        calls += 1
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"echo": value.payload["flag"]}),
            usage={"total_tokens": 0},
        )

    kwargs = {
        "channel_id": channel_id,
        "capability": "candidate_analysis",
        "provider": "mock",
        "model": "fixture",
        "prompt_version": "fixture-v1",
        "handler": handler,
        "run_key": "fixture-boolean-integer-identity",
    }
    first_id, first_output = await harness.execute(
        **kwargs,
        input_value=AgentInputEnvelope(payload={"flag": True}),
    )
    assert first_output.payload == {"echo": True}

    with pytest.raises(AgentHarnessError, match="different logical request"):
        await harness.execute(
            **kwargs,
            input_value=AgentInputEnvelope(payload={"flag": 1}),
        )
    assert calls == 1
    assert harness.inspect(first_id)["output"] == {"payload": {"echo": True}}


@pytest.mark.asyncio
async def test_nonfinite_json_values_fail_closed_instead_of_collapsing_to_null(
    database: Database,
    settings: Settings,
) -> None:
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
    harness = IntelligenceAgentHarness(database)

    async def handler(_value: AgentInputEnvelope, _attempt: int) -> AgentStepResult:
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"ok": True}),
            usage={"total_tokens": 0},
        )

    kwargs = {
        "channel_id": channel_id,
        "capability": "candidate_analysis",
        "provider": "mock",
        "model": "fixture",
        "prompt_version": "fixture-v1",
    }
    with pytest.raises(ValueError, match="finite JSON numbers"):
        await harness.execute(
            **kwargs,
            handler=handler,
            input_value=AgentInputEnvelope(payload={"value": float("nan")}),
            run_key="fixture-nonfinite-input",
        )
    with database.session() as session:
        assert (
            session.scalar(
                select(func.count(IntelligenceAgentRun.id)).where(
                    IntelligenceAgentRun.run_key == "fixture-nonfinite-input"
                )
            )
            == 0
        )

    async def nonfinite_output(_value: AgentInputEnvelope, _attempt: int) -> AgentStepResult:
        return AgentStepResult(
            output=AgentOutputEnvelope(payload={"value": float("inf")}),
            usage={"total_tokens": 0},
        )

    with pytest.raises(AgentHarnessError, match="failed after"):
        await harness.execute(
            **kwargs,
            input_value=AgentInputEnvelope(payload={"value": None}),
            handler=nonfinite_output,
            run_key="fixture-nonfinite-output",
        )
    with database.session() as session:
        failed_id = session.scalar(
            select(IntelligenceAgentRun.id).where(
                IntelligenceAgentRun.run_key == "fixture-nonfinite-output"
            )
        )
    assert failed_id is not None
    detail = harness.inspect(failed_id)
    assert detail["status"] == "failed"
    assert "finite JSON numbers" in str(detail["error_summary"])
