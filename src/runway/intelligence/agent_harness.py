from __future__ import annotations

import asyncio
import json
import math
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, ClassVar, TypeVar, cast

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

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


class AgentRunConflictError(AgentHarnessError):
    pass


RunResultT = TypeVar("RunResultT")


def _json_values_equal(left: Any, right: Any) -> bool:
    """Compare parsed JSON values without Python's bool/int equality collision."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, (int, float)) and isinstance(right, (int, float)):
        return bool(left == right)
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        if left.keys() != right.keys():
            return False
        return all(_json_values_equal(left[key], right[key]) for key in left)
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_values_equal(left_item, right_item)
            for left_item, right_item in zip(left, right, strict=True)
        )
    return bool(left == right)


def _require_json_compatible(value: Any, *, path: str) -> None:
    """Reject values whose JSON serialization would lose their logical identity."""
    if value is None or isinstance(value, (str, bool)):
        return
    if type(value) is int:
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite JSON numbers")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _require_json_compatible(item, path=f"{path}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{path} must contain only string object keys")
            _require_json_compatible(item, path=f"{path}.{key}")
        return
    raise ValueError(f"{path} must contain only JSON-serializable values")


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


class AgentPipelineStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capability: str
    input_value: AgentInputEnvelope
    route: str = "standard"
    required: bool = True


class AdaptiveEditorialPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    risk_route: str
    reasons: list[str]
    steps: list[AgentPipelineStep]


class AdaptiveEditorialRouter:
    """Choose an auditable route; handlers remain explicit and provider-controlled."""

    @staticmethod
    def plan(
        *,
        input_value: AgentInputEnvelope,
        image_fact_confidence: float,
        retrieval_coverage: float,
        rank_separation: float,
        factual_claim_risk: bool,
        identity_conflict: bool,
    ) -> AdaptiveEditorialPlan:
        reasons: list[str] = []
        if factual_claim_risk:
            reasons.append("factual_claim_risk")
        if identity_conflict:
            reasons.append("identity_conflict")
        if image_fact_confidence < 0.65:
            reasons.append("low_image_fact_confidence")
        if retrieval_coverage < 0.5:
            reasons.append("weak_retrieval_coverage")
        if rank_separation < 0.08:
            reasons.append("close_candidate_scores")
        if factual_claim_risk or identity_conflict:
            route = "high_risk"
        elif reasons:
            route = "uncertain"
        else:
            route = "easy"
        capabilities = [
            "retrieve_evidence",
            "evidence_coverage",
            "content_mode_selection",
            "editorial_angle_planning",
            "caption_generation",
            "caption_verification",
        ]
        if route != "easy" or factual_claim_risk:
            capabilities.append("claim_level_grounding")
        capabilities.extend(["joint_multimodal_reranking", "slate_diversity_optimization"])
        return AdaptiveEditorialPlan(
            risk_route=route,
            reasons=reasons or ["high_confidence_clear_case"],
            steps=[
                AgentPipelineStep(
                    capability=capability,
                    input_value=input_value,
                    route=route,
                    required=True,
                )
                for capability in capabilities
            ],
        )


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
            ("retrieve_evidence", "Retrieve channel-scoped editorial evidence."),
            ("evidence_coverage", "Evaluate whether retrieved evidence is sufficient."),
            ("content_mode_selection", "Choose a learned channel content mode."),
            ("editorial_angle_planning", "Plan distinct editorial angle lanes."),
            ("caption_generation", "Generate a grounded caption candidate slate."),
            ("caption_verification", "Verify visible evidence and policy constraints."),
            ("claim_level_grounding", "Classify atomic caption claims against evidence."),
            ("joint_multimodal_reranking", "Jointly score image-caption combinations."),
            ("slate_diversity_optimization", "Optimize a complete diverse editorial slate."),
            (
                "shadow_editorial_simulation",
                "Recommend an isolated editorial decision without changing creator labels.",
            ),
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


@dataclass(frozen=True)
class _RunClaim:
    run_id: int
    lease_owner: str | None
    initial_attempt_count: int
    replay_output_json: str | None = None


class IntelligenceAgentHarness:
    """Persisted, lease-protected execution for deterministic intelligence work.

    A run key names one immutable logical request. Terminal completed/abstained
    rows are replayed, failed rows can be retried by the identical request, and
    running rows are owned through a renewable lease so a crashed worker can be
    recovered without weakening the unique-key duplicate guard.
    """

    def __init__(
        self,
        database: Database,
        registry: IntelligenceCapabilityRegistry | None = None,
        *,
        lease_seconds: float = 30.0,
        clock: Callable[[], datetime] = utcnow,
    ):
        if not math.isfinite(lease_seconds) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive finite duration")
        self.database = database
        self.registry = registry or IntelligenceCapabilityRegistry()
        self.lease_seconds = float(lease_seconds)
        self._clock = clock

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    def _now(self) -> datetime:
        return self._as_utc(self._clock())

    def _lease_expiry(self) -> datetime:
        return self._now() + timedelta(seconds=self.lease_seconds)

    @staticmethod
    def _normalized_run_key(run_key: str | None, fallback_prefix: str) -> str:
        if run_key is None:
            return f"{fallback_prefix}-{uuid.uuid4().hex}"
        key = run_key.strip()
        if not key:
            raise ValueError("run_key must be non-empty when supplied")
        if len(key) > 128:
            raise ValueError("run_key must fit the persisted 128-character identity")
        return key

    @staticmethod
    def _rowcount(result: object) -> int:
        return cast(CursorResult[Any], result).rowcount

    @staticmethod
    def _json_equal(left: str, right: str) -> bool:
        try:
            left_value: object = json.loads(left)
            right_value: object = json.loads(right)
            return _json_values_equal(left_value, right_value)
        except ValueError:
            return False

    def _ensure_same_logical_request(
        self,
        existing: IntelligenceAgentRun,
        *,
        key: str,
        channel_id: int,
        input_json: str,
        config_hash: str,
    ) -> None:
        if (
            existing.channel_id != channel_id
            or existing.configuration_hash != config_hash
            or not self._json_equal(existing.input_json, input_json)
        ):
            raise AgentRunConflictError(
                f"agent run {key!r} already belongs to a different logical request"
            )

    def _claim_existing_run(
        self,
        session: Session,
        existing: IntelligenceAgentRun,
        *,
        key: str,
        channel_id: int,
        input_json: str,
        budget_json: str,
        config_hash: str,
        owner: str,
    ) -> _RunClaim:
        self._ensure_same_logical_request(
            existing,
            key=key,
            channel_id=channel_id,
            input_json=input_json,
            config_hash=config_hash,
        )
        if existing.status in {"completed", "abstained"}:
            return _RunClaim(
                run_id=existing.id,
                lease_owner=None,
                initial_attempt_count=existing.attempt_count,
                replay_output_json=existing.output_json,
            )
        if existing.status not in {"failed", "running"}:
            raise AgentRunConflictError(
                f"agent run {key!r} already exists with unsupported status {existing.status!r}"
            )

        now = self._now()
        lease_expiry = self._lease_expiry()
        if existing.status == "failed":
            result = session.execute(
                update(IntelligenceAgentRun)
                .where(
                    IntelligenceAgentRun.id == existing.id,
                    IntelligenceAgentRun.status == "failed",
                )
                .values(
                    status="running",
                    output_json="{}",
                    usage_json=AgentUsage().model_dump_json(),
                    error_summary=None,
                    completed_at=None,
                    lease_owner=owner,
                    lease_expires_at=lease_expiry,
                    input_json=input_json,
                    budget_json=budget_json,
                    configuration_hash=config_hash,
                )
                .execution_options(synchronize_session=False)
            )
        else:
            stored_expiry = existing.lease_expires_at
            expired = stored_expiry is None or self._as_utc(stored_expiry) <= now
            if not expired:
                raise AgentRunConflictError(
                    f"agent run {key!r} is already claimed by another execution"
                )
            lease_predicate = (
                IntelligenceAgentRun.lease_expires_at.is_(None)
                if stored_expiry is None
                else and_(
                    IntelligenceAgentRun.lease_expires_at == stored_expiry,
                    IntelligenceAgentRun.lease_expires_at <= now,
                )
            )
            result = session.execute(
                update(IntelligenceAgentRun)
                .where(
                    IntelligenceAgentRun.id == existing.id,
                    IntelligenceAgentRun.status == "running",
                    lease_predicate,
                )
                .values(
                    status="running",
                    output_json="{}",
                    usage_json=AgentUsage().model_dump_json(),
                    error_summary=None,
                    completed_at=None,
                    lease_owner=owner,
                    lease_expires_at=lease_expiry,
                    input_json=input_json,
                    budget_json=budget_json,
                    configuration_hash=config_hash,
                )
                .execution_options(synchronize_session=False)
            )
        if self._rowcount(result) != 1:
            raise AgentRunConflictError(f"agent run {key!r} was claimed by a concurrent execution")
        session.execute(
            update(IntelligenceAgentStep)
            .where(
                IntelligenceAgentStep.agent_run_id == existing.id,
                IntelligenceAgentStep.status == "running",
            )
            .values(
                status="failed",
                error_summary="stale lease recovered before retry",
                completed_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        return _RunClaim(
            run_id=existing.id,
            lease_owner=owner,
            initial_attempt_count=existing.attempt_count,
        )

    @staticmethod
    def _is_duplicate_key_error(exc: IntegrityError) -> bool:
        message = str(exc).casefold()
        return (
            "uq_intelligence_agent_run_key" in message
            or "intelligence_agent_runs.run_key" in message
        )

    def _claim_run(
        self,
        *,
        channel_id: int,
        key: str,
        capability: str,
        provider: str,
        model: str,
        prompt_version: str,
        input_json: str,
        budget_json: str,
        config_hash: str,
    ) -> _RunClaim:
        owner = f"lease-{uuid.uuid4().hex}"
        for claim_attempt in range(2):
            try:
                with self.database.session() as session:
                    existing = session.scalar(
                        select(IntelligenceAgentRun)
                        .where(IntelligenceAgentRun.run_key == key)
                        .limit(1)
                    )
                    if existing is not None:
                        return self._claim_existing_run(
                            session,
                            existing,
                            key=key,
                            channel_id=channel_id,
                            input_json=input_json,
                            budget_json=budget_json,
                            config_hash=config_hash,
                            owner=owner,
                        )
                    run = IntelligenceAgentRun(
                        channel_id=channel_id,
                        run_key=key,
                        capability=capability,
                        provider=provider,
                        model=model,
                        prompt_version=prompt_version,
                        status="running",
                        input_json=input_json,
                        output_json="{}",
                        budget_json=budget_json,
                        usage_json=AgentUsage().model_dump_json(),
                        configuration_hash=config_hash,
                        attempt_count=0,
                        lease_owner=owner,
                        lease_expires_at=self._lease_expiry(),
                    )
                    session.add(run)
                    session.flush()
                    return _RunClaim(
                        run_id=run.id,
                        lease_owner=owner,
                        initial_attempt_count=0,
                    )
            except IntegrityError as exc:
                if not self._is_duplicate_key_error(exc):
                    raise
            except OperationalError as exc:
                message = str(exc).casefold()
                if "locked" not in message and "snapshot" not in message:
                    raise
                if claim_attempt == 1:
                    raise AgentRunConflictError(
                        f"agent run {key!r} is contended by a concurrent execution"
                    ) from exc
        raise AgentRunConflictError(
            f"concurrent creation of agent run {key!r} could not be reconciled"
        )

    def _next_attempt_number(self, run_id: int, sequence: int) -> int:
        with self.database.session() as session:
            highest = session.scalar(
                select(func.max(IntelligenceAgentStep.attempt)).where(
                    IntelligenceAgentStep.agent_run_id == run_id,
                    IntelligenceAgentStep.sequence == sequence,
                )
            )
        return int(highest or 0) + 1

    def _renew_lease_in_session(
        self,
        session: Session,
        *,
        run_id: int,
        owner: str,
    ) -> bool:
        result = session.execute(
            update(IntelligenceAgentRun)
            .where(
                IntelligenceAgentRun.id == run_id,
                IntelligenceAgentRun.lease_owner == owner,
                IntelligenceAgentRun.status == "running",
            )
            .values(lease_expires_at=self._lease_expiry())
            .execution_options(synchronize_session=False)
        )
        return self._rowcount(result) == 1

    def _require_lease_in_session(
        self,
        session: Session,
        *,
        run_id: int,
        owner: str,
    ) -> None:
        if not self._renew_lease_in_session(session, run_id=run_id, owner=owner):
            raise AgentRunConflictError(
                f"agent run {run_id} lease ownership was lost during execution"
            )

    def _renew_lease(self, run_id: int, owner: str) -> bool | None:
        try:
            with self.database.session() as session:
                return self._renew_lease_in_session(session, run_id=run_id, owner=owner)
        except SQLAlchemyError:
            return None

    async def _heartbeat_lease(self, run_id: int, owner: str) -> None:
        while True:
            await asyncio.sleep(max(0.01, self.lease_seconds / 2))
            renewed = self._renew_lease(run_id, owner)
            if renewed is False:
                return

    async def _with_lease_heartbeat(
        self,
        run_id: int,
        owner: str,
        operation: Callable[[], Awaitable[RunResultT]],
    ) -> RunResultT:
        heartbeat = asyncio.create_task(self._heartbeat_lease(run_id, owner))
        try:
            return await operation()
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    def _begin_step(
        self,
        *,
        run_id: int,
        owner: str,
        sequence: int,
        attempt: int,
        capability: str,
        input_json: str,
        budget_json: str,
        total_tokens: int,
        timeout_seconds: float,
        run_attempt_count: int,
    ) -> int:
        with self.database.session() as session:
            self._require_lease_in_session(session, run_id=run_id, owner=owner)
            persisted_run = session.get(IntelligenceAgentRun, run_id)
            if persisted_run is None:
                raise AgentHarnessError("persisted agent run disappeared")
            persisted_run.attempt_count = run_attempt_count
            step = IntelligenceAgentStep(
                agent_run_id=run_id,
                sequence=sequence,
                attempt=attempt,
                capability=capability,
                status="running",
                input_json=input_json,
                output_json="{}",
                budget_json=budget_json,
                usage_json=AgentUsage(total_tokens=total_tokens).model_dump_json(),
                timeout_seconds=max(1, int(timeout_seconds)),
            )
            session.add(step)
            session.flush()
            return step.id

    def _finish_step_failed(
        self,
        step_id: int,
        message: str,
        total_tokens: int,
        *,
        owner: str,
    ) -> None:
        with self.database.session() as session:
            step = session.get(IntelligenceAgentStep, step_id)
            if step is not None:
                self._require_lease_in_session(
                    session,
                    run_id=step.agent_run_id,
                    owner=owner,
                )
                step.status = "failed"
                step.error_summary = message[:2000]
                step.usage_json = AgentUsage(total_tokens=total_tokens).model_dump_json()
                step.completed_at = self._now()

    def _finish_failed(
        self,
        run_id: int,
        message: str,
        total_tokens: int,
        attempt_count: int,
        *,
        owner: str,
    ) -> None:
        with self.database.session() as session:
            result = session.execute(
                update(IntelligenceAgentRun)
                .where(
                    IntelligenceAgentRun.id == run_id,
                    IntelligenceAgentRun.lease_owner == owner,
                    IntelligenceAgentRun.status == "running",
                )
                .values(
                    status="failed",
                    error_summary=message[:4000],
                    usage_json=AgentUsage(total_tokens=total_tokens).model_dump_json(),
                    attempt_count=attempt_count,
                    completed_at=self._now(),
                    lease_owner=None,
                    lease_expires_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            if self._rowcount(result) != 1:
                raise AgentRunConflictError(
                    f"agent run {run_id} lease ownership was lost before failure persistence"
                )

    def _finish_single_success(
        self,
        *,
        run_id: int,
        step_id: int,
        owner: str,
        terminal_status: str,
        output: AgentOutputEnvelope,
        usage: AgentUsage,
        total_tokens: int,
        attempt_count: int,
        artifact_type: str | None,
        artifact_id: str | None,
        abstention_reason: str | None,
    ) -> None:
        completed_at = self._now()
        with self.database.session() as session:
            result = session.execute(
                update(IntelligenceAgentRun)
                .where(
                    IntelligenceAgentRun.id == run_id,
                    IntelligenceAgentRun.lease_owner == owner,
                    IntelligenceAgentRun.status == "running",
                )
                .values(
                    status=terminal_status,
                    output_json=output.model_dump_json(),
                    usage_json=usage.model_copy(
                        update={"total_tokens": total_tokens}
                    ).model_dump_json(),
                    attempt_count=attempt_count,
                    error_summary=abstention_reason,
                    completed_at=completed_at,
                    lease_owner=None,
                    lease_expires_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            if self._rowcount(result) != 1:
                raise AgentRunConflictError(
                    f"agent run {run_id} lease ownership was lost before completion"
                )
            persisted_step = session.get(IntelligenceAgentStep, step_id)
            if persisted_step is None:
                raise AgentHarnessError("persisted agent step disappeared")
            persisted_step.status = terminal_status
            persisted_step.output_json = output.model_dump_json()
            persisted_step.usage_json = usage.model_dump_json()
            persisted_step.artifact_type = artifact_type
            persisted_step.artifact_id = artifact_id
            persisted_step.error_summary = abstention_reason
            persisted_step.completed_at = completed_at

    def _finish_pipeline_step(
        self,
        *,
        step_id: int,
        owner: str,
        terminal_status: str,
        output: AgentOutputEnvelope,
        usage: AgentUsage,
        artifact_type: str | None,
        artifact_id: str | None,
        abstention_reason: str | None,
    ) -> None:
        with self.database.session() as session:
            persisted = session.get(IntelligenceAgentStep, step_id)
            if persisted is None:
                raise AgentHarnessError("persisted pipeline step disappeared")
            self._require_lease_in_session(
                session,
                run_id=persisted.agent_run_id,
                owner=owner,
            )
            persisted.status = terminal_status
            persisted.output_json = output.model_dump_json()
            persisted.usage_json = usage.model_dump_json()
            persisted.artifact_type = artifact_type
            persisted.artifact_id = artifact_id
            persisted.error_summary = abstention_reason
            persisted.completed_at = self._now()

    def _finish_pipeline_terminal(
        self,
        *,
        run_id: int,
        owner: str,
        terminal_status: str,
        output_json: str,
        total_tokens: int,
        attempt_count: int,
        error_summary: str | None,
    ) -> None:
        with self.database.session() as session:
            result = session.execute(
                update(IntelligenceAgentRun)
                .where(
                    IntelligenceAgentRun.id == run_id,
                    IntelligenceAgentRun.lease_owner == owner,
                    IntelligenceAgentRun.status == "running",
                )
                .values(
                    status=terminal_status,
                    output_json=output_json,
                    usage_json=AgentUsage(total_tokens=total_tokens).model_dump_json(),
                    attempt_count=attempt_count,
                    error_summary=error_summary,
                    completed_at=self._now(),
                    lease_owner=None,
                    lease_expires_at=None,
                )
                .execution_options(synchronize_session=False)
            )
            if self._rowcount(result) != 1:
                raise AgentRunConflictError(
                    f"agent run {run_id} lease ownership was lost before pipeline completion"
                )

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
        _require_json_compatible(
            validated_input.model_dump(mode="python"),
            path="agent input",
        )
        effective_budget = budget or AgentBudget()
        key = self._normalized_run_key(run_key, "agent")
        configuration = {
            "capability": capability,
            "provider": provider,
            "model": model,
            "prompt_version": prompt_version,
            "budget": effective_budget.model_dump(),
        }
        config_hash = configuration_hash(configuration)
        input_json = validated_input.model_dump_json()
        budget_json = effective_budget.model_dump_json()
        claim = self._claim_run(
            channel_id=channel_id,
            key=key,
            capability=capability,
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            input_json=input_json,
            budget_json=budget_json,
            config_hash=config_hash,
        )
        if claim.replay_output_json is not None:
            payload: object = json.loads(claim.replay_output_json)
            output = spec.output_type.model_validate(payload)
            return claim.run_id, output
        owner = claim.lease_owner
        if owner is None:
            raise AssertionError("a newly claimed agent run must have a lease owner")
        run_id = claim.run_id

        async def perform() -> tuple[int, AgentOutputEnvelope]:
            start = time.monotonic()
            total_tokens = 0
            errors: list[str] = []
            attempts = min(
                effective_budget.max_attempts_per_step,
                effective_budget.max_steps,
            )
            first_attempt = self._next_attempt_number(run_id, sequence=1)
            for offset in range(attempts):
                attempt = first_attempt + offset
                elapsed = time.monotonic() - start
                if elapsed >= effective_budget.max_seconds:
                    message = "agent wall-clock budget was exhausted before the next attempt"
                    self._finish_failed(
                        run_id,
                        message,
                        total_tokens,
                        attempt - 1,
                        owner=owner,
                    )
                    raise AgentBudgetExceeded(message)
                timeout = min(
                    effective_budget.timeout_seconds,
                    max(0.001, effective_budget.max_seconds - elapsed),
                )
                step_id = self._begin_step(
                    run_id=run_id,
                    owner=owner,
                    sequence=1,
                    attempt=attempt,
                    capability=capability,
                    input_json=input_json,
                    budget_json=budget_json,
                    total_tokens=total_tokens,
                    timeout_seconds=timeout,
                    run_attempt_count=attempt,
                )
                try:
                    result = await asyncio.wait_for(
                        handler(validated_input, attempt),
                        timeout=timeout,
                    )
                    output = spec.output_type.model_validate(result.output)
                    _require_json_compatible(
                        output.model_dump(mode="python"),
                        path=f"{capability} output",
                    )
                    usage = self._normalized_usage(result.usage)
                    total_tokens += usage.total_tokens
                    if total_tokens > effective_budget.max_total_tokens:
                        raise AgentBudgetExceeded(
                            "agent token budget was exceeded; no fallback was attempted"
                        )
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    errors.append(message)
                    self._finish_step_failed(step_id, message, total_tokens, owner=owner)
                    if isinstance(exc, AgentBudgetExceeded):
                        self._finish_failed(
                            run_id,
                            message,
                            total_tokens,
                            attempt,
                            owner=owner,
                        )
                        raise
                    if offset == attempts - 1:
                        self._finish_failed(
                            run_id,
                            "; ".join(errors),
                            total_tokens,
                            attempt,
                            owner=owner,
                        )
                        raise AgentHarnessError(
                            f"agent capability {capability!r} failed after {attempts} attempt(s)"
                        ) from exc
                    continue

                terminal_status = (
                    "abstained" if result.abstention_reason is not None else "completed"
                )
                self._finish_single_success(
                    run_id=run_id,
                    step_id=step_id,
                    owner=owner,
                    terminal_status=terminal_status,
                    output=output,
                    usage=usage,
                    total_tokens=total_tokens,
                    attempt_count=attempt,
                    artifact_type=result.artifact_type,
                    artifact_id=result.artifact_id,
                    abstention_reason=result.abstention_reason,
                )
                return run_id, output

            raise AssertionError("agent harness exhausted an unreachable execution path")

        return await self._with_lease_heartbeat(run_id, owner, perform)

    async def execute_pipeline(
        self,
        *,
        channel_id: int,
        steps: list[AgentPipelineStep],
        handlers: dict[str, AgentHandler],
        provider: str,
        model: str,
        prompt_version: str,
        risk_route: str,
        budget: AgentBudget | None = None,
        run_key: str | None = None,
    ) -> tuple[int, dict[str, AgentOutputEnvelope]]:
        """Execute multiple real capabilities under one persisted parent run."""

        if not steps:
            raise ValueError("editorial agent pipeline requires at least one step")
        effective_budget = budget or AgentBudget(max_steps=max(3, len(steps)))
        if len(steps) > effective_budget.max_steps:
            raise AgentBudgetExceeded("pipeline contains more steps than the configured budget")
        if risk_route not in {"easy", "uncertain", "high_risk"}:
            raise ValueError("risk_route must be easy, uncertain, or high_risk")
        for step in steps:
            self.registry.resolve(step.capability)
            if step.capability not in handlers:
                raise LookupError(f"pipeline handler for {step.capability!r} is missing")
            _require_json_compatible(
                step.input_value.model_dump(mode="python"),
                path=f"pipeline input for {step.capability!r}",
            )
        key = self._normalized_run_key(run_key, "editorial-pipeline")
        configuration = {
            "version": "adaptive-editorial-pipeline-v1",
            "capability": "editorial_pipeline",
            "provider": provider,
            "model": model,
            "prompt_version": prompt_version,
            "risk_route": risk_route,
            "steps": [step.model_dump(mode="json") for step in steps],
            "budget": effective_budget.model_dump(),
        }
        input_json = json.dumps(
            {
                "risk_route": risk_route,
                "steps": [step.model_dump(mode="json") for step in steps],
            },
            sort_keys=True,
        )
        budget_json = effective_budget.model_dump_json()
        claim = self._claim_run(
            channel_id=channel_id,
            key=key,
            capability="editorial_pipeline",
            provider=provider,
            model=model,
            prompt_version=prompt_version,
            input_json=input_json,
            budget_json=budget_json,
            config_hash=configuration_hash(configuration),
        )
        if claim.replay_output_json is not None:
            payload = json.loads(claim.replay_output_json)
            if not isinstance(payload, dict):
                raise AgentHarnessError("persisted pipeline output is malformed")
            return claim.run_id, {
                str(name): AgentOutputEnvelope.model_validate(value)
                for name, value in payload.items()
            }
        owner = claim.lease_owner
        if owner is None:
            raise AssertionError("a newly claimed pipeline run must have a lease owner")
        run_id = claim.run_id

        async def perform() -> tuple[int, dict[str, AgentOutputEnvelope]]:
            started = time.monotonic()
            total_tokens = 0
            total_attempts = claim.initial_attempt_count
            outputs: dict[str, AgentOutputEnvelope] = {}
            previous: AgentOutputEnvelope | None = None
            for sequence, pipeline_step in enumerate(steps, start=1):
                spec = self.registry.resolve(pipeline_step.capability)
                payload = dict(pipeline_step.input_value.payload)
                if previous is not None:
                    payload["pipeline_previous_output"] = previous.payload
                step_input = spec.input_type.model_validate(
                    pipeline_step.input_value.model_copy(update={"payload": payload})
                )
                _require_json_compatible(
                    step_input.model_dump(mode="python"),
                    path=f"pipeline input for {pipeline_step.capability!r}",
                )
                step_input_json = step_input.model_dump_json()
                errors: list[str] = []
                completed = False
                first_attempt = self._next_attempt_number(run_id, sequence)
                for offset in range(effective_budget.max_attempts_per_step):
                    attempt = first_attempt + offset
                    elapsed = time.monotonic() - started
                    if elapsed >= effective_budget.max_seconds:
                        message = "pipeline wall-clock budget was exhausted"
                        self._finish_failed(
                            run_id,
                            message,
                            total_tokens,
                            total_attempts,
                            owner=owner,
                        )
                        raise AgentBudgetExceeded(message)
                    total_attempts += 1
                    timeout = min(
                        effective_budget.timeout_seconds,
                        max(0.001, effective_budget.max_seconds - elapsed),
                    )
                    step_id = self._begin_step(
                        run_id=run_id,
                        owner=owner,
                        sequence=sequence,
                        attempt=attempt,
                        capability=pipeline_step.capability,
                        input_json=step_input_json,
                        budget_json=budget_json,
                        total_tokens=total_tokens,
                        timeout_seconds=timeout,
                        run_attempt_count=total_attempts,
                    )
                    artifact_type: str | None = None
                    artifact_id: str | None = None
                    abstention_reason: str | None = None
                    output = AgentOutputEnvelope(payload={})
                    usage = AgentUsage()
                    terminal_status = "failed"
                    try:
                        result = await asyncio.wait_for(
                            handlers[pipeline_step.capability](step_input, attempt),
                            timeout=timeout,
                        )
                        output = spec.output_type.model_validate(result.output)
                        _require_json_compatible(
                            output.model_dump(mode="python"),
                            path=f"{pipeline_step.capability} output",
                        )
                        usage = self._normalized_usage(result.usage)
                        total_tokens += usage.total_tokens
                        if total_tokens > effective_budget.max_total_tokens:
                            raise AgentBudgetExceeded("pipeline token budget was exceeded")
                        artifact_type = result.artifact_type
                        artifact_id = result.artifact_id
                        abstention_reason = result.abstention_reason
                    except Exception as exc:
                        message = f"{type(exc).__name__}: {exc}"
                        errors.append(message)
                        self._finish_step_failed(step_id, message, total_tokens, owner=owner)
                        if isinstance(exc, AgentBudgetExceeded):
                            self._finish_failed(
                                run_id,
                                message,
                                total_tokens,
                                total_attempts,
                                owner=owner,
                            )
                            raise
                        if offset < effective_budget.max_attempts_per_step - 1:
                            continue
                        if pipeline_step.required:
                            self._finish_failed(
                                run_id,
                                "; ".join(errors),
                                total_tokens,
                                total_attempts,
                                owner=owner,
                            )
                            raise AgentHarnessError(
                                f"required pipeline step {pipeline_step.capability!r} failed"
                            ) from exc
                        output = AgentOutputEnvelope(
                            payload={
                                "skipped": True,
                                "errors": errors,
                                "route": pipeline_step.route,
                            }
                        )
                        usage = AgentUsage(total_tokens=0)
                        terminal_status = "skipped"
                    else:
                        terminal_status = (
                            "abstained" if abstention_reason is not None else "completed"
                        )
                    self._finish_pipeline_step(
                        step_id=step_id,
                        owner=owner,
                        terminal_status=terminal_status,
                        output=output,
                        usage=usage,
                        artifact_type=artifact_type,
                        artifact_id=artifact_id,
                        abstention_reason=abstention_reason,
                    )
                    outputs[pipeline_step.capability] = output
                    previous = output
                    completed = True
                    if terminal_status == "abstained":
                        self._finish_pipeline_terminal(
                            run_id=run_id,
                            owner=owner,
                            terminal_status="abstained",
                            output_json=json.dumps(
                                {name: value.model_dump() for name, value in outputs.items()},
                                sort_keys=True,
                            ),
                            total_tokens=total_tokens,
                            attempt_count=total_attempts,
                            error_summary=abstention_reason,
                        )
                        return run_id, outputs
                    break
                if not completed:
                    raise AssertionError("pipeline step exhausted without a terminal state")

            self._finish_pipeline_terminal(
                run_id=run_id,
                owner=owner,
                terminal_status="completed",
                output_json=json.dumps(
                    {name: value.model_dump() for name, value in outputs.items()},
                    sort_keys=True,
                ),
                total_tokens=total_tokens,
                attempt_count=total_attempts,
                error_summary=None,
            )
            return run_id, outputs

        return await self._with_lease_heartbeat(run_id, owner, perform)

    def persist_observed_pipeline(
        self,
        *,
        channel_id: int,
        plan: AdaptiveEditorialPlan,
        outputs: dict[str, AgentOutputEnvelope],
        provider: str,
        model: str,
        prompt_version: str,
        run_key: str,
        terminal_status: str,
        artifact_type: str,
        artifact_id: str | int,
        usage: dict[str, object] | None = None,
        terminal_reason: str | None = None,
    ) -> int:
        """Persist an honest parent trace for work completed by the canonical pipeline.

        The caption path performs retrieval, planning, verification, and deterministic
        ranking in-process while generation is already captured as child agent runs.
        This method records those observed results under one parent without re-running
        providers or representing the trace as a separate model execution.
        """

        if terminal_status not in {"completed", "abstained"}:
            raise ValueError("observed pipeline status must be completed or abstained")
        if terminal_status == "abstained" and not (terminal_reason or "").strip():
            raise ValueError("an abstained observed pipeline requires a terminal reason")
        if not run_key.strip() or not artifact_type.strip():
            raise ValueError("run_key and artifact_type are required")
        capabilities = [step.capability for step in plan.steps]
        if len(capabilities) != len(set(capabilities)):
            raise ValueError("an observed pipeline cannot contain duplicate capabilities")
        missing = [capability for capability in capabilities if capability not in outputs]
        extra = sorted(set(outputs).difference(capabilities))
        if missing or extra:
            raise ValueError(
                f"observed pipeline outputs do not match the plan; missing={missing}, extra={extra}"
            )
        validated_outputs: dict[str, AgentOutputEnvelope] = {}
        for capability in capabilities:
            spec = self.registry.resolve(capability)
            validated_outputs[capability] = spec.output_type.model_validate(outputs[capability])
            _require_json_compatible(
                validated_outputs[capability].model_dump(mode="python"),
                path=f"observed {capability} output",
            )
        for pipeline_step in plan.steps:
            _require_json_compatible(
                pipeline_step.input_value.model_dump(mode="python"),
                path=f"observed {pipeline_step.capability} input",
            )

        normalized_usage = self._normalized_usage(usage or {})
        budget = AgentBudget(
            max_steps=max(1, len(plan.steps)),
            max_attempts_per_step=1,
            max_total_tokens=max(100_000, normalized_usage.total_tokens),
        )
        configuration = {
            "version": "adaptive-editorial-pipeline-v1",
            "execution_mode": "observed_canonical_trace",
            "provider": provider,
            "model": model,
            "prompt_version": prompt_version,
            "risk_route": plan.risk_route,
            "reasons": plan.reasons,
            "steps": [step.model_dump(mode="json") for step in plan.steps],
            "artifact_type": artifact_type.strip(),
            "artifact_id": str(artifact_id),
        }
        config_hash = configuration_hash(configuration)
        output_payload = {
            capability: value.model_dump() for capability, value in validated_outputs.items()
        }
        completed_at = utcnow()
        try:
            with self.database.session() as session:
                existing = session.scalar(
                    select(IntelligenceAgentRun)
                    .where(IntelligenceAgentRun.run_key == run_key.strip())
                    .limit(1)
                )
                if existing is not None:
                    if (
                        existing.channel_id != channel_id
                        or existing.configuration_hash != config_hash
                        or existing.status != terminal_status
                        or not _json_values_equal(json.loads(existing.output_json), output_payload)
                    ):
                        raise AgentHarnessError(
                            f"observed pipeline {run_key!r} conflicts with its persisted trace"
                        )
                    return existing.id

                run = IntelligenceAgentRun(
                    channel_id=channel_id,
                    run_key=run_key.strip(),
                    capability="editorial_pipeline",
                    provider=provider,
                    model=model,
                    prompt_version=prompt_version,
                    status=terminal_status,
                    input_json=json.dumps(
                        {
                            "execution_mode": "observed_canonical_trace",
                            "risk_route": plan.risk_route,
                            "reasons": plan.reasons,
                            "steps": [step.model_dump(mode="json") for step in plan.steps],
                        },
                        sort_keys=True,
                    ),
                    output_json=json.dumps(output_payload, sort_keys=True),
                    budget_json=budget.model_dump_json(),
                    usage_json=normalized_usage.model_dump_json(),
                    configuration_hash=config_hash,
                    attempt_count=len(plan.steps),
                    error_summary=terminal_reason,
                    completed_at=completed_at,
                )
                session.add(run)
                session.flush()
                run_id = run.id
                for sequence, pipeline_step in enumerate(plan.steps, start=1):
                    is_terminal = sequence == len(plan.steps)
                    step_status = terminal_status if is_terminal else "completed"
                    step_usage = (
                        normalized_usage
                        if pipeline_step.capability == "caption_generation"
                        else AgentUsage()
                    )
                    session.add(
                        IntelligenceAgentStep(
                            agent_run_id=run_id,
                            parent_step_id=None,
                            sequence=sequence,
                            attempt=1,
                            capability=pipeline_step.capability,
                            status=step_status,
                            input_json=pipeline_step.input_value.model_dump_json(),
                            output_json=validated_outputs[
                                pipeline_step.capability
                            ].model_dump_json(),
                            budget_json=budget.model_dump_json(),
                            usage_json=step_usage.model_dump_json(),
                            timeout_seconds=max(1, int(budget.timeout_seconds)),
                            artifact_type=artifact_type.strip() if is_terminal else None,
                            artifact_id=str(artifact_id) if is_terminal else None,
                            error_summary=terminal_reason if is_terminal else None,
                            completed_at=completed_at,
                        )
                    )
        except IntegrityError as exc:
            if not self._is_duplicate_key_error(exc):
                raise
            with self.database.session() as session:
                existing = session.scalar(
                    select(IntelligenceAgentRun)
                    .where(IntelligenceAgentRun.run_key == run_key.strip())
                    .limit(1)
                )
                if existing is not None:
                    if (
                        existing.channel_id != channel_id
                        or existing.configuration_hash != config_hash
                        or existing.status != terminal_status
                        or not _json_values_equal(json.loads(existing.output_json), output_payload)
                    ):
                        raise AgentHarnessError(
                            f"observed pipeline {run_key!r} conflicts with its persisted trace"
                        ) from exc
                    return existing.id
            raise AgentRunConflictError(
                f"concurrent creation of observed pipeline {run_key!r} could not be reconciled"
            ) from exc
        except OperationalError as exc:
            message = str(exc).casefold()
            if "locked" not in message and "snapshot" not in message:
                raise
            raise AgentRunConflictError(
                f"observed pipeline {run_key!r} is contended by a concurrent creation"
            ) from exc
        return run_id

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
                "lease_owner": run.lease_owner,
                "lease_expires_at": (
                    run.lease_expires_at.isoformat() if run.lease_expires_at is not None else None
                ),
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
