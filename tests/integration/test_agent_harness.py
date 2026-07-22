from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select

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
    AgentStepResult,
    CapabilitySpec,
    IntelligenceAgentHarness,
    IntelligenceCapabilityRegistry,
)


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

    with pytest.raises(AgentHarnessError, match="failed after"):
        await harness.execute(
            channel_id=channel_id,
            capability="caption_verification",
            provider="mock",
            model="fixture",
            prompt_version="fixture-v1",
            input_value=AgentInputEnvelope(payload={"caption": "fixture"}),
            handler=slow,
            budget=AgentBudget(
                max_steps=1,
                max_attempts_per_step=1,
                max_total_tokens=10,
                max_seconds=1,
                timeout_seconds=0.01,
            ),
        )


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
