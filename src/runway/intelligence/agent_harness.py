from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from runway.db.base import Database
from runway.db.models import (
    IntelligenceAgentRun,
    IntelligenceAgentStep,
    utcnow,
)
from runway.intelligence.embeddings import configuration_hash


class AgentHarnessError(RuntimeError):
    pass


class AgentBudgetExceeded(AgentHarnessError):
    pass


class AgentInputEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_ids: list[int] = Field(default_factory=list)
    payload: dict[str, object]


class AgentOutputEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    payload: dict[str, object]


class AgentBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_steps: int = Field(default=3, ge=1, le=100)
    max_attempts_per_step: int = Field(default=2, ge=1, le=10)
    max_total_tokens: int = Field(default=100_000, ge=0)
    max_seconds: float = Field(default=300.0, gt=0, le=3600)
    timeout_seconds: float = Field(default=120.0, gt=0, le=1800)


class AgentUsage(BaseModel):
    model_config = ConfigDict(extra="allow")

    total_tokens: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class AgentStepResult:
    output: AgentOutputEnvelope
    usage: dict[str, object]
    artifact_type: str | None = None
    artifact_id: str | None = None
    abstention_reason: str | None = None

    def __post_init__(self) -> None:
        if self.abstention_reason is not None and not self.abstention_reason.strip():
            raise ValueError("abstention_reason must be non-empty when supplied")


@dataclass(frozen=True)
class CapabilitySpec:
    name: str
    input_type: type[AgentInputEnvelope]
    output_type: type[AgentOutputEnvelope]
    description: str


class IntelligenceCapabilityRegistry:
    """Explicit safe capabilities; publishing is intentionally absent."""

    FORBIDDEN_TOKENS: ClassVar[tuple[str, ...]] = (
        "publish",
        "publisher",
        "schedule_post",
        "delete_post",
        "remove_post",
        "youtube_mutation",
    )

    def __init__(self) -> None:
        self._specs: dict[str, CapabilitySpec] = {}
        for name, description in (
            ("historical_annotation", "Create typed historical annotation candidates."),
            ("candidate_analysis", "Analyze a candidate image without mutating platforms."),
            ("retrieval_planning", "Plan channel-scoped retrieval constraints."),
            ("caption_generation", "Generate a grounded caption candidate slate."),
            ("caption_verification", "Verify visible evidence and policy constraints."),
            ("image_generation_mock", "Create deterministic local image fixtures only."),
        ):
            self.register(
                CapabilitySpec(
                    name=name,
                    input_type=AgentInputEnvelope,
                    output_type=AgentOutputEnvelope,
                    description=description,
                )
            )

    def register(self, spec: CapabilitySpec) -> None:
        normalized = spec.name.strip().casefold()
        if not normalized or normalized != spec.name:
            raise ValueError("capability names must be normalized non-empty text")
        if any(token in normalized for token in self.FORBIDDEN_TOKENS):
            raise ValueError("publishing capabilities are forbidden in the intelligence harness")
        if normalized in self._specs:
            raise ValueError(f"capability {normalized!r} is already registered")
        self._specs[normalized] = spec

    def resolve(self, name: str) -> CapabilitySpec:
        try:
            return self._specs[name]
        except KeyError as exc:
            raise LookupError(
                f"intelligence capability {name!r} is not registered; no fallback was used"
            ) from exc

    def status(self) -> dict[str, object]:
        return {
            "capabilities": [
                {
                    "name": spec.name,
                    "description": spec.description,
                    "input_type": spec.input_type.__name__,
                    "output_type": spec.output_type.__name__,
                }
                for spec in sorted(self._specs.values(), key=lambda item: item.name)
            ],
            "publishing_capability_present": False,
            "provider_fallback_enabled": False,
        }


AgentHandler = Callable[
    [AgentInputEnvelope, int],
    Awaitable[AgentStepResult],
]


class IntelligenceAgentHarness:
    def __init__(
        self,
        database: Database,
        registry: IntelligenceCapabilityRegistry | None = None,
    ):
        self.database = database
        self.registry = registry or IntelligenceCapabilityRegistry()

    async def execute(
        self,
        *,
        channel_id: int,
        capability: str,
        provider: str,
        model: str,
        prompt_version: str,
        input_value: AgentInputEnvelope,
        handler: AgentHandler,
        budget: AgentBudget | None = None,
        run_key: str | None = None,
    ) -> tuple[int, AgentOutputEnvelope]:
        spec = self.registry.resolve(capability)
        validated_input = spec.input_type.model_validate(input_value)
        effective_budget = budget or AgentBudget()
        key = run_key or f"agent-{uuid.uuid4().hex}"
        configuration = {
            "capability": capability,
            "provider": provider,
            "model": model,
            "prompt_version": prompt_version,
            "budget": effective_budget.model_dump(),
        }
        config_hash = configuration_hash(configuration)
        with self.database.session() as session:
            existing = session.scalar(
                select(IntelligenceAgentRun).where(IntelligenceAgentRun.run_key == key).limit(1)
            )
            if existing is not None:
                if existing.status not in {"completed", "abstained"}:
                    raise AgentHarnessError(
                        f"agent run {key!r} already exists with status {existing.status}"
                    )
                payload: object = json.loads(existing.output_json)
                output = spec.output_type.model_validate(payload)
                return existing.id, output
            run = IntelligenceAgentRun(
                channel_id=channel_id,
                run_key=key,
                capability=capability,
                provider=provider,
                model=model,
                prompt_version=prompt_version,
                status="running",
                input_json=validated_input.model_dump_json(),
                output_json="{}",
                budget_json=effective_budget.model_dump_json(),
                usage_json=AgentUsage().model_dump_json(),
                configuration_hash=config_hash,
                attempt_count=0,
            )
            session.add(run)
            session.flush()
            run_id = run.id

        start = time.monotonic()
        total_tokens = 0
        errors: list[str] = []
        attempts = min(
            effective_budget.max_attempts_per_step,
            effective_budget.max_steps,
        )
        for attempt in range(1, attempts + 1):
            elapsed = time.monotonic() - start
            if elapsed >= effective_budget.max_seconds:
                message = "agent wall-clock budget was exhausted before the next attempt"
                self._finish_failed(run_id, message, total_tokens, attempt - 1)
                raise AgentBudgetExceeded(message)
            with self.database.session() as session:
                step = IntelligenceAgentStep(
                    agent_run_id=run_id,
                    sequence=1,
                    attempt=attempt,
                    capability=capability,
                    status="running",
                    input_json=validated_input.model_dump_json(),
                    output_json="{}",
                    budget_json=effective_budget.model_dump_json(),
                    usage_json=AgentUsage(total_tokens=total_tokens).model_dump_json(),
                    timeout_seconds=max(
                        1,
                        int(
                            min(
                                effective_budget.timeout_seconds,
                                effective_budget.max_seconds - elapsed,
                            )
                        ),
                    ),
                )
                session.add(step)
                session.flush()
                step_id = step.id
                persisted_run = session.get(IntelligenceAgentRun, run_id)
                if persisted_run is not None:
                    persisted_run.attempt_count = attempt
            timeout = min(
                effective_budget.timeout_seconds,
                max(0.001, effective_budget.max_seconds - elapsed),
            )
            try:
                result = await asyncio.wait_for(
                    handler(validated_input, attempt),
                    timeout=timeout,
                )
                output = spec.output_type.model_validate(result.output)
                usage = self._normalized_usage(result.usage)
                total_tokens += usage.total_tokens
                if total_tokens > effective_budget.max_total_tokens:
                    raise AgentBudgetExceeded(
                        "agent token budget was exceeded; no fallback was attempted"
                    )
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                errors.append(message)
                self._finish_step_failed(
                    step_id,
                    message,
                    total_tokens,
                )
                if isinstance(exc, AgentBudgetExceeded):
                    self._finish_failed(run_id, message, total_tokens, attempt)
                    raise
                if attempt == attempts:
                    self._finish_failed(
                        run_id,
                        "; ".join(errors),
                        total_tokens,
                        attempt,
                    )
                    raise AgentHarnessError(
                        f"agent capability {capability!r} failed after {attempt} attempt(s)"
                    ) from exc
                continue

            completed_at = utcnow()
            terminal_status = "abstained" if result.abstention_reason is not None else "completed"
            with self.database.session() as session:
                persisted_step = session.get(IntelligenceAgentStep, step_id)
                if persisted_step is None:
                    raise AgentHarnessError("persisted agent step disappeared")
                persisted_step.status = terminal_status
                persisted_step.output_json = output.model_dump_json()
                persisted_step.usage_json = usage.model_dump_json()
                persisted_step.artifact_type = result.artifact_type
                persisted_step.artifact_id = result.artifact_id
                persisted_step.error_summary = result.abstention_reason
                persisted_step.completed_at = completed_at
                persisted_run = session.get(IntelligenceAgentRun, run_id)
                if persisted_run is None:
                    raise AgentHarnessError("persisted agent run disappeared")
                persisted_run.status = terminal_status
                persisted_run.output_json = output.model_dump_json()
                persisted_run.usage_json = usage.model_copy(
                    update={"total_tokens": total_tokens}
                ).model_dump_json()
                persisted_run.completed_at = completed_at
                persisted_run.error_summary = result.abstention_reason
            return run_id, output

        raise AssertionError("agent harness exhausted an unreachable execution path")

    def inspect(self, run_id: int) -> dict[str, object]:
        with self.database.session() as session:
            run = session.get(IntelligenceAgentRun, run_id)
            if run is None:
                raise LookupError(f"agent run {run_id} was not found")
            steps = session.scalars(
                select(IntelligenceAgentStep)
                .where(IntelligenceAgentStep.agent_run_id == run_id)
                .order_by(
                    IntelligenceAgentStep.sequence,
                    IntelligenceAgentStep.attempt,
                )
            ).all()
            return {
                "id": run.id,
                "run_key": run.run_key,
                "capability": run.capability,
                "provider": run.provider,
                "model": run.model,
                "prompt_version": run.prompt_version,
                "status": run.status,
                "input": json.loads(run.input_json),
                "output": json.loads(run.output_json),
                "budget": json.loads(run.budget_json),
                "usage": json.loads(run.usage_json),
                "attempt_count": run.attempt_count,
                "error_summary": run.error_summary,
                "steps": [
                    {
                        "id": step.id,
                        "sequence": step.sequence,
                        "attempt": step.attempt,
                        "capability": step.capability,
                        "status": step.status,
                        "input": json.loads(step.input_json),
                        "output": json.loads(step.output_json),
                        "budget": json.loads(step.budget_json),
                        "usage": json.loads(step.usage_json),
                        "timeout_seconds": step.timeout_seconds,
                        "artifact_type": step.artifact_type,
                        "artifact_id": step.artifact_id,
                        "error_summary": step.error_summary,
                    }
                    for step in steps
                ],
            }

    def link_artifact(
        self,
        run_id: int,
        *,
        artifact_type: str,
        artifact_id: str | int,
    ) -> None:
        if not artifact_type.strip():
            raise ValueError("artifact_type is required")
        with self.database.session() as session:
            run = session.get(IntelligenceAgentRun, run_id)
            if run is None:
                raise LookupError(f"agent run {run_id} was not found")
            step = session.scalar(
                select(IntelligenceAgentStep)
                .where(
                    IntelligenceAgentStep.agent_run_id == run_id,
                    IntelligenceAgentStep.status == "completed",
                )
                .order_by(
                    IntelligenceAgentStep.sequence.desc(),
                    IntelligenceAgentStep.attempt.desc(),
                )
                .limit(1)
            )
            if step is None:
                raise ValueError("only a completed agent step can link an artifact")
            step.artifact_type = artifact_type.strip()
            step.artifact_id = str(artifact_id)

    @staticmethod
    def _normalized_usage(value: dict[str, object]) -> AgentUsage:
        raw = value.get("total_tokens")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            prompt = value.get("prompt_tokens", 0)
            completion = value.get("completion_tokens", 0)
            raw = (prompt if isinstance(prompt, (int, float)) else 0) + (
                completion if isinstance(completion, (int, float)) else 0
            )
        payload = dict(value)
        payload["total_tokens"] = max(0, int(raw))
        return AgentUsage.model_validate(payload)

    def _finish_step_failed(
        self,
        step_id: int,
        message: str,
        total_tokens: int,
    ) -> None:
        with self.database.session() as session:
            step = session.get(IntelligenceAgentStep, step_id)
            if step is not None:
                step.status = "failed"
                step.error_summary = message[:2000]
                step.usage_json = AgentUsage(total_tokens=total_tokens).model_dump_json()
                step.completed_at = utcnow()

    def _finish_failed(
        self,
        run_id: int,
        message: str,
        total_tokens: int,
        attempt_count: int,
    ) -> None:
        with self.database.session() as session:
            run = session.get(IntelligenceAgentRun, run_id)
            if run is not None:
                run.status = "failed"
                run.error_summary = message[:4000]
                run.usage_json = AgentUsage(total_tokens=total_tokens).model_dump_json()
                run.attempt_count = attempt_count
                run.completed_at = utcnow()
