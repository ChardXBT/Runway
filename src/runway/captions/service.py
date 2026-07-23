from __future__ import annotations

import json
import math
import time
from datetime import UTC, datetime
from typing import Any, cast

from sqlalchemy import select

from runway.analysis.features import (
    caption_features,
    channel_style_score,
)
from runway.analysis.runtime import AgentRuntime, AgentRuntimeError, runtime_for
from runway.analysis.schemas import (
    CandidateAnalysis,
    CaptionCandidate,
    CaptionCandidateSet,
    CaptionGroundingAssessment,
    CaptionGroundingAudit,
    CaptionOptions,
)
from runway.captions.claim_grounding import (
    ClaimLevelGroundingVerifier,
    normalize_open_question_answer_uncertainty,
)
from runway.captions.feature_snapshots import (
    FEATURE_SCHEMA_VERSION,
    TAXONOMY_VERSION,
    VERIFIER_VERSION,
    snapshot_json_and_hash,
)
from runway.captions.feedback import CaptionFeedbackService
from runway.captions.planning import EditorialBrief, EditorialPlanner
from runway.captions.preferences import (
    DeterministicImageCaptionPairRanker,
    PairwiseCaptionPreferenceRanker,
    PreferenceScore,
)
from runway.captions.taxonomy import analyze_caption, normalize_caption
from runway.captions.verification import CaptionVerifier, VerificationResult
from runway.captions.visual_consensus import (
    VisualConsensusReport,
    candidate_analysis_from_mapping,
    reconcile_visual_audit_sequence,
    trusted_source_evidence,
)
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CandidateImage,
    CaptionCandidateRecord,
    CaptionSlate,
    MediaAsset,
    ModelRun,
    Post,
    Proposal,
    RetrievalEvidenceRecord,
    SearchRun,
    utcnow,
)
from runway.db.repositories import audit, get_channel
from runway.intelligence.agent_harness import (
    AdaptiveEditorialRouter,
    AgentBudget,
    AgentInputEnvelope,
    AgentOutputEnvelope,
    AgentStepResult,
    IntelligenceAgentHarness,
)
from runway.intelligence.embeddings import (
    ActiveRepresentationResolver,
    RepresentationStore,
    TextEmbeddingProvider,
    configuration_hash,
    content_hash,
)
from runway.intelligence.policies import PolicySnapshot
from runway.intelligence.reranking import JointMultimodalReranker
from runway.intelligence.retrieval import RetrievalService
from runway.media.service import prepare_model_detail_views

GENERIC_PATTERNS = (
    "what do you think",
    "comment below",
    "like and subscribe",
    "is very ",
    "looks very ",
    "seems very ",
)


def _required_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError(f"{field} must be an integer")
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be an integer") from exc


def _accumulate_usage(
    total: dict[str, object],
    addition: dict[str, object],
) -> dict[str, object]:
    """Add one model call to a nested usage summary without losing provider fields."""

    def merge(first: object, second: object) -> object:
        if (
            isinstance(first, (int, float))
            and not isinstance(first, bool)
            and isinstance(second, (int, float))
            and not isinstance(second, bool)
        ):
            return first + second
        if isinstance(first, dict) and isinstance(second, dict):
            combined: dict[str, object] = {str(key): value for key, value in first.items()}
            for key, value in second.items():
                normalized_key = str(key)
                combined[normalized_key] = (
                    merge(combined[normalized_key], value) if normalized_key in combined else value
                )
            return combined
        return second

    merged = cast(dict[str, object], merge(total, addition))
    prior_calls = total.get("model_calls", 0)
    merged["model_calls"] = (
        int(prior_calls)
        if isinstance(prior_calls, (int, float)) and not isinstance(prior_calls, bool)
        else 0
    ) + 1
    return merged


class CaptionService:
    prompt_version = "captions-v6"
    generation_configuration: dict[str, Any] = {
        "version": "canonical-caption-pipeline-4-ensemble-grounding",
        "candidate_limit": 12,
        "display_limit": 3,
        "retry_limit": 1,
        "grounding_threshold": 0.85,
        "diversity_similarity_threshold": 0.82,
        "weights": {
            "preference": 0.20,
            "grounding": 0.28,
            "policy": 0.12,
            "style": 0.07,
            "novelty": 0.09,
            "rotation": 0.06,
            "positive_feedback": 0.06,
            "pairing": 0.09,
            "negative_feedback_risk": -0.08,
            "generic_penalty": -0.12,
        },
    }

    def __init__(
        self,
        database: Database,
        settings: Settings,
        runtime: AgentRuntime | None = None,
    ):
        self.database = database
        self.settings = settings
        self.runtime = runtime or runtime_for(settings)
        self.retrieval = RetrievalService(database, settings)
        self.feedback = CaptionFeedbackService(database, settings)
        self.planner = EditorialPlanner()
        self.verifier = CaptionVerifier()
        self.claim_verifier = ClaimLevelGroundingVerifier()
        self.preference_ranker = PairwiseCaptionPreferenceRanker(database)
        self.pair_ranker = DeterministicImageCaptionPairRanker()
        self.representations = ActiveRepresentationResolver(database)
        self.agent_harness = IntelligenceAgentHarness(database)

    async def generate(self, candidate_id: int) -> CaptionOptions:
        with self.database.session() as session:
            configured_channel = get_channel(session, self.settings.channel_handle)
            candidate = session.get(CandidateImage, candidate_id)
            if candidate is None:
                raise LookupError(f"candidate {candidate_id} not found")
            if candidate.hard_rejection_reason:
                raise ValueError("captions cannot be generated for a hard-rejected candidate")
            media = session.get(MediaAsset, candidate.media_asset_id)
            search_run = session.get(SearchRun, candidate.search_run_id)
            if media is None or search_run is None:
                raise LookupError(f"candidate {candidate_id} has incomplete source data")
            if search_run.channel_id != configured_channel.id:
                raise LookupError(f"candidate {candidate_id} not found")
            channel_id = search_run.channel_id
            image_path = self.settings.resolved_data_dir / media.local_path
            primary_analysis = candidate_analysis_from_mapping(
                self._json_dict(candidate.detected_topic_json)
            )
            source_evidence = trusted_source_evidence(
                source_domain=candidate.source_domain,
                provider_result_json=candidate.provider_result_json,
            )
            source_context: dict[str, object] = {
                "candidate_id": candidate.id,
                "media_asset_id": media.id,
                "source_page_url": candidate.source_page_url,
                "source_domain": candidate.source_domain,
                "rights_status": candidate.rights_status,
                "trusted_source_evidence": source_evidence,
            }
            image_score = candidate.final_rank_score

        pipeline_usage: dict[str, object] = {}
        detail_image_paths = [
            str(path)
            for path in prepare_model_detail_views(
                image_path,
                self.settings,
            )
        ]
        audit_analysis, visual_audit_run_id, visual_audit_usage = await self._audit_visual_analysis(
            channel_id=channel_id,
            candidate_id=candidate_id,
            image_path=str(image_path),
            source_domain=candidate.source_domain,
            source_evidence=source_evidence,
            primary=primary_analysis,
            detail_image_paths=detail_image_paths,
        )
        pipeline_usage = _accumulate_usage(pipeline_usage, visual_audit_usage)
        visual_audits = [audit_analysis]
        visual_audit_run_ids = [visual_audit_run_id] if visual_audit_run_id is not None else []
        initial_consensus = reconcile_visual_audit_sequence(
            primary_analysis,
            visual_audits,
            source_evidence=source_evidence,
        )
        if self._requires_visual_tiebreak(initial_consensus):
            tiebreak_analysis, tiebreak_run_id, tiebreak_usage = await self._audit_visual_analysis(
                channel_id=channel_id,
                candidate_id=candidate_id,
                image_path=str(image_path),
                source_domain=candidate.source_domain,
                source_evidence=source_evidence,
                primary=primary_analysis,
                detail_image_paths=detail_image_paths,
            )
            visual_audits.append(tiebreak_analysis)
            if tiebreak_run_id is not None:
                visual_audit_run_ids.append(tiebreak_run_id)
            pipeline_usage = _accumulate_usage(pipeline_usage, tiebreak_usage)
        visual_consensus = reconcile_visual_audit_sequence(
            primary_analysis,
            visual_audits,
            source_evidence=source_evidence,
        )
        analysis = visual_consensus.analysis.model_dump()
        source_context["visual_consensus"] = visual_consensus.model_dump()

        context = self.retrieval.context_for_candidate(
            media.id,
            candidate_id=candidate_id,
        )
        policy = PolicySnapshot.model_validate(context["policy"])
        brief = self.planner.build(
            candidate_analysis=analysis,
            retrieval_context=context,
            policy=policy,
            source_context=source_context,
            visual_consensus=visual_consensus,
        )
        caption_examples = cast(
            list[dict[str, Any]],
            context["caption_style_examples"],
        )
        historical_ids = [int(item["post_id"]) for item in caption_examples]
        payload: dict[str, object] = {
            "candidate_id": candidate_id,
            "candidate_analysis": analysis,
            "visual_consensus": {
                "confirmed_facts": [fact.model_dump() for fact in visual_consensus.confirmed_facts],
                "disputed_facts": [fact.model_dump() for fact in visual_consensus.disputed_facts],
                "trusted_source_evidence": source_evidence,
            },
            "historical_post_ids": historical_ids,
            "retrieval_context": context,
            "editorial_brief": brief.model_dump(),
            "angle_lanes": [
                {
                    "lane": angle,
                    "instruction": (
                        "Generate at least one materially distinct candidate in this lane."
                    ),
                }
                for angle in brief.recommended_angles
            ],
            "_image_path": str(image_path),
            "_image_paths": detail_image_paths,
        }
        started_at = utcnow()
        started_perf = time.perf_counter()
        attempts: list[CaptionCandidateSet] = []
        agent_run_ids: list[int] = []
        generated, agent_run_id, generation_usage = await self._generate_with_agent(
            channel_id=channel_id,
            candidate_id=candidate_id,
            payload=payload,
        )
        pipeline_usage = _accumulate_usage(pipeline_usage, generation_usage)
        agent_run_ids.append(agent_run_id)
        attempts.append(generated)
        ranked, first_grounding_run_id, first_grounding_usage = await self._ranked_candidates(
            channel_id=channel_id,
            candidate_id=candidate_id,
            generated=generated,
            brief=brief,
            context=context,
            analysis=analysis,
            allowed_reference_ids=historical_ids,
            image_score=image_score,
            attempt_number=1,
            image_path=str(image_path),
            trusted_source_evidence=source_evidence,
            detail_image_paths=detail_image_paths,
        )
        pipeline_usage = _accumulate_usage(pipeline_usage, first_grounding_usage)
        grounding_audit_run_ids = [first_grounding_run_id] if first_grounding_run_id else []
        if len(self._eligible_rows(ranked)) < 3:
            retry_payload = {
                **payload,
                "retry": {
                    "reason": "fewer than three grounded, policy-compliant candidates survived",
                    "failed_candidates": [
                        {
                            "text": row["candidate"].text,
                            "verifier": row["verification"].model_dump(),
                            "independent_grounding": row["grounding_assessment"].model_dump(),
                        }
                        for row in ranked
                        if not row["eligible"]
                    ],
                    "instruction": (
                        "Use only the brief's highest-confidence visible facts and vary structure."
                    ),
                },
            }
            (
                generated_retry,
                retry_agent_run_id,
                retry_generation_usage,
            ) = await self._generate_with_agent(
                channel_id=channel_id,
                candidate_id=candidate_id,
                payload=retry_payload,
            )
            pipeline_usage = _accumulate_usage(pipeline_usage, retry_generation_usage)
            agent_run_ids.append(retry_agent_run_id)
            attempts.append(generated_retry)
            (
                retry_ranked,
                retry_grounding_run_id,
                retry_grounding_usage,
            ) = await self._ranked_candidates(
                channel_id=channel_id,
                candidate_id=candidate_id,
                generated=generated_retry,
                brief=brief,
                context=context,
                analysis=analysis,
                allowed_reference_ids=historical_ids,
                image_score=image_score,
                attempt_number=2,
                prior_candidate_texts=[
                    cast(CaptionCandidate, row["candidate"]).text for row in ranked
                ],
                image_path=str(image_path),
                trusted_source_evidence=source_evidence,
                detail_image_paths=detail_image_paths,
            )
            pipeline_usage = _accumulate_usage(pipeline_usage, retry_grounding_usage)
            if retry_grounding_run_id is not None:
                grounding_audit_run_ids.append(retry_grounding_run_id)
            ranked.extend(retry_ranked)
            ranked = self._sort_ranked(ranked)

        latency_ms = round((time.perf_counter() - started_perf) * 1000, 3)
        eligible = self._eligible_rows(ranked)
        if len(eligible) < 3:
            evidence_provenance = self._evidence_provenance(
                channel_id=channel_id,
                context=context,
                attempts=attempts,
                ranked=ranked,
            )
            evidence_provenance["independent_verification"] = {
                "visual_audit_model_run_ids": visual_audit_run_ids,
                "caption_grounding_model_run_ids": grounding_audit_run_ids,
                "consensus_coverage": visual_consensus.consensus_coverage,
                "disputed_facts": [fact.model_dump() for fact in visual_consensus.disputed_facts],
            }
            slate_id = self._persist_abstention(
                channel_id=channel_id,
                candidate_id=candidate_id,
                brief=brief,
                attempts=attempts,
                ranked=ranked,
                context=context,
                payload=payload,
                started_at=started_at,
                latency_ms=latency_ms,
                reason="no three grounded, policy-compliant, distinct captions survived",
                agent_run_ids=agent_run_ids,
                evidence_provenance=evidence_provenance,
                usage=pipeline_usage,
            )
            for run_id in agent_run_ids:
                self.agent_harness.link_artifact(
                    run_id,
                    artifact_type="caption_slate",
                    artifact_id=slate_id,
                )
            self._persist_adaptive_trace(
                channel_id=channel_id,
                candidate_id=candidate_id,
                analysis=analysis,
                context=context,
                brief=brief,
                ranked=ranked,
                displayed=[],
                agent_run_ids=agent_run_ids,
                slate_id=slate_id,
                terminal_status="abstained",
                terminal_reason=("no three grounded, policy-compliant, distinct captions survived"),
                usage=pipeline_usage,
            )
            return CaptionOptions(
                recommended="",
                alternatives=[],
                rationale="Runway abstained instead of displaying generic filler.",
                confidence=0.0,
                referenced_historical_post_ids=[],
                factual_uncertainty_warning=("No grounded caption slate survived verification."),
                slate_id=slate_id,
                retrieval_run_id=_required_int(
                    context["retrieval_run_id"],
                    "retrieval_run_id",
                ),
                abstained=True,
                abstention_reason=(
                    "no three grounded, policy-compliant, distinct captions survived"
                ),
            )

        displayed = self._select_display_slate(eligible, policy, channel_id=channel_id)
        cited_references = [
            value
            for value in attempts[-1].referenced_historical_post_ids
            if value in set(historical_ids)
        ]
        evidence_provenance = self._evidence_provenance(
            channel_id=channel_id,
            context=context,
            attempts=attempts,
            ranked=ranked,
        )
        evidence_provenance["independent_verification"] = {
            "visual_audit_model_run_ids": visual_audit_run_ids,
            "caption_grounding_model_run_ids": grounding_audit_run_ids,
            "consensus_coverage": visual_consensus.consensus_coverage,
            "disputed_facts": [fact.model_dump() for fact in visual_consensus.disputed_facts],
        }
        result, slate_id = self._persist_success(
            channel_id=channel_id,
            candidate_id=candidate_id,
            brief=brief,
            attempts=attempts,
            ranked=ranked,
            displayed=displayed,
            context=context,
            payload=payload,
            started_at=started_at,
            latency_ms=latency_ms,
            cited_references=cited_references,
            agent_run_ids=agent_run_ids,
            evidence_provenance=evidence_provenance,
            usage=pipeline_usage,
        )
        for run_id in agent_run_ids:
            self.agent_harness.link_artifact(
                run_id,
                artifact_type="caption_slate",
                artifact_id=slate_id,
            )
        self._persist_adaptive_trace(
            channel_id=channel_id,
            candidate_id=candidate_id,
            analysis=analysis,
            context=context,
            brief=brief,
            ranked=ranked,
            displayed=displayed,
            agent_run_ids=agent_run_ids,
            slate_id=slate_id,
            terminal_status="completed",
            usage=pipeline_usage,
        )
        result.slate_id = slate_id
        return result

    def _persist_adaptive_trace(
        self,
        *,
        channel_id: int,
        candidate_id: int,
        analysis: dict[str, Any],
        context: dict[str, object],
        brief: EditorialBrief,
        ranked: list[dict[str, Any]],
        displayed: list[dict[str, Any]],
        agent_run_ids: list[int],
        slate_id: int,
        terminal_status: str,
        terminal_reason: str | None = None,
        usage: dict[str, object],
    ) -> int:
        retrieval_run_id = _required_int(
            context["retrieval_run_id"],
            "retrieval_run_id",
        )
        with self.database.session() as session:
            selected_rows = session.scalars(
                select(RetrievalEvidenceRecord)
                .where(
                    RetrievalEvidenceRecord.retrieval_run_id == retrieval_run_id,
                    RetrievalEvidenceRecord.selected.is_(True),
                )
                .order_by(
                    RetrievalEvidenceRecord.selected_rank,
                    RetrievalEvidenceRecord.id,
                )
            ).all()
            slate = session.get(CaptionSlate, slate_id)
            reranker_result = self._json_dict(slate.reranker_run_json) if slate is not None else {}
        selected_evidence: list[dict[str, object]] = [
            {
                "retrieval_evidence_id": row.id,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "retrieval_channel": row.retrieval_channel,
                "selected_rank": row.selected_rank,
                "evidence_role": row.evidence_role,
            }
            for row in selected_rows
        ]
        selected_count = len(selected_evidence)
        retrieval_coverage = min(1.0, selected_count / 6) if selected_evidence else 0.0
        eligible_scores = [float(row["final_score"]) for row in ranked if bool(row["eligible"])]
        rank_separation = (
            eligible_scores[0] - eligible_scores[1] if len(eligible_scores) > 1 else 0.0
        )
        identity_values = {
            fact.value.casefold() for fact in brief.uncertain_facts if fact.field == "entity"
        }
        factual_claim_risk = bool(brief.uncertain_facts) or any(
            bool(row["claim_verification"].critical_failures) for row in ranked
        )
        input_value = AgentInputEnvelope(
            entity_ids=[candidate_id],
            payload={
                "candidate_id": candidate_id,
                "caption_slate_id": slate_id,
                "retrieval_run_id": context.get("retrieval_run_id"),
            },
        )
        plan = AdaptiveEditorialRouter.plan(
            input_value=input_value,
            image_fact_confidence=max(
                0.0,
                min(1.0, float(analysis.get("confidence") or 0.0)),
            ),
            retrieval_coverage=retrieval_coverage,
            rank_separation=max(0.0, rank_separation),
            factual_claim_risk=factual_claim_risk,
            identity_conflict=len(identity_values) > 1,
        )
        claim_rows = [
            {
                "caption": row["candidate"].text,
                **row["claim_verification"].as_dict(),
            }
            for row in ranked
        ]
        displayed_rows = [
            {
                "caption": row["candidate"].text,
                "editorial_angle": row["candidate"].editorial_angle,
                "final_score": row["final_score"],
            }
            for row in displayed
        ]
        outputs: dict[str, AgentOutputEnvelope] = {
            "retrieve_evidence": AgentOutputEnvelope(
                payload={
                    "retrieval_run_id": context.get("retrieval_run_id"),
                    "selected_count": selected_count,
                    "selected_evidence": selected_evidence,
                }
            ),
            "evidence_coverage": AgentOutputEnvelope(
                payload={
                    "coverage": retrieval_coverage,
                    "selected_count": selected_count,
                }
            ),
            "content_mode_selection": AgentOutputEnvelope(payload=brief.content_mode),
            "editorial_angle_planning": AgentOutputEnvelope(
                payload={
                    "possible_angles": brief.possible_editorial_angles,
                    "angle_lanes": brief.recommended_angles,
                }
            ),
            "caption_generation": AgentOutputEnvelope(
                payload={
                    "child_agent_run_ids": agent_run_ids,
                    "candidate_count": len(ranked),
                    "angles_produced": sorted({row["candidate"].editorial_angle for row in ranked}),
                }
            ),
            "caption_verification": AgentOutputEnvelope(
                payload={
                    "eligible_count": len(self._eligible_rows(ranked)),
                    "candidate_count": len(ranked),
                    "results": [
                        {
                            "caption": row["candidate"].text,
                            "eligible": row["eligible"],
                            "verification": row["verification"].model_dump(),
                        }
                        for row in ranked
                    ],
                }
            ),
            "claim_level_grounding": AgentOutputEnvelope(payload={"results": claim_rows}),
            "joint_multimodal_reranking": AgentOutputEnvelope(payload=reranker_result),
            "slate_diversity_optimization": AgentOutputEnvelope(
                payload={
                    "displayed": displayed_rows,
                    "abstained": terminal_status == "abstained",
                    "reason": terminal_reason,
                }
            ),
        }
        planned_outputs = {step.capability: outputs[step.capability] for step in plan.steps}
        return self.agent_harness.persist_observed_pipeline(
            channel_id=channel_id,
            plan=plan,
            outputs=planned_outputs,
            provider=self.runtime.provider,
            model=self.runtime.model_name,
            prompt_version=self.prompt_version,
            run_key=f"caption-slate-{slate_id}-adaptive-v1",
            terminal_status=terminal_status,
            artifact_type="caption_slate",
            artifact_id=slate_id,
            usage=usage,
            terminal_reason=terminal_reason,
        )

    async def _generate_with_agent(
        self,
        *,
        channel_id: int,
        candidate_id: int,
        payload: dict[str, object],
    ) -> tuple[CaptionCandidateSet, int, dict[str, object]]:
        observed_usage: dict[str, object] = {}

        async def handler(
            envelope: AgentInputEnvelope,
            _attempt: int,
        ) -> AgentStepResult:
            nonlocal observed_usage
            generated = await self.runtime.generate_caption_options(envelope.payload)
            observed_usage = dict(self.runtime.last_token_usage)
            return AgentStepResult(
                output=AgentOutputEnvelope(payload=generated.model_dump()),
                usage=observed_usage,
            )

        run_id, output = await self.agent_harness.execute(
            channel_id=channel_id,
            capability="caption_generation",
            provider=self.runtime.provider,
            model=self.runtime.model_name,
            prompt_version=self.prompt_version,
            input_value=AgentInputEnvelope(
                entity_ids=[candidate_id],
                payload=payload,
            ),
            handler=handler,
            budget=AgentBudget(
                max_steps=1,
                max_attempts_per_step=1,
                max_total_tokens=100_000,
                max_seconds=float(self.settings.codex_timeout_seconds),
                timeout_seconds=float(self.settings.codex_timeout_seconds),
            ),
        )
        return CaptionCandidateSet.model_validate(output.payload), run_id, observed_usage

    async def _audit_visual_analysis(
        self,
        *,
        channel_id: int,
        candidate_id: int,
        image_path: str,
        source_domain: str | None,
        source_evidence: dict[str, object],
        primary: CandidateAnalysis,
        detail_image_paths: list[str],
    ) -> tuple[CandidateAnalysis, int | None, dict[str, object]]:
        method = getattr(self.runtime, "audit_candidate_image", None)
        if not callable(method):
            if self.settings.agent_runtime != "mock":
                raise AgentRuntimeError(
                    "the configured runtime cannot perform independent visual audits"
                )
            return primary.model_copy(deep=True), None, {}
        payload: dict[str, object] = {
            "candidate_id": candidate_id,
            "source_domain": source_domain,
            "trusted_source_evidence": source_evidence,
            "_image_path": image_path,
            "_image_paths": detail_image_paths,
        }
        started_at = utcnow()
        audit_analysis = await method(payload)
        usage = dict(self.runtime.last_token_usage)
        model_run_id = self._persist_runtime_model_run(
            channel_id=channel_id,
            candidate_id=candidate_id,
            task_type="audit_candidate_image",
            prompt_version="candidate-analysis-blind-v1",
            request={key: value for key, value in payload.items() if not key.startswith("_")},
            output=audit_analysis.model_dump(),
            token_usage=usage,
            started_at=started_at,
        )
        return audit_analysis, model_run_id, usage

    @staticmethod
    def _requires_visual_tiebreak(consensus: VisualConsensusReport) -> bool:
        risky_action_markers = (
            "argu",
            "driv",
            "eat",
            "hold",
            "kiss",
            "read",
            "sleep",
            "watch",
            "writ",
        )
        actions = " ".join(consensus.analysis.actions).casefold()
        return (
            consensus.consensus_coverage < 0.8
            or bool(consensus.disputed_facts)
            or any(marker in actions for marker in risky_action_markers)
        )

    async def _audit_caption_grounding(
        self,
        *,
        channel_id: int,
        candidate_id: int,
        image_path: str,
        prepared: list[tuple[int, CaptionCandidate, list[str]]],
        trusted_source_evidence: dict[str, object],
        detail_image_paths: list[str],
    ) -> tuple[CaptionGroundingAudit, int | None, dict[str, object]]:
        candidates = [
            {"candidate_id": generation_index, "text": candidate.text}
            for generation_index, candidate, _reasons in prepared
        ]
        if not candidates:
            raise ValueError("caption grounding requires at least one prepared candidate")
        payload: dict[str, object] = {
            "candidates": candidates,
            "trusted_source_evidence": trusted_source_evidence,
            "_image_path": image_path,
            "_image_paths": detail_image_paths,
        }
        method = getattr(self.runtime, "verify_caption_grounding", None)
        if not callable(method):
            if self.settings.agent_runtime != "mock":
                raise AgentRuntimeError(
                    "the configured runtime cannot perform independent caption grounding"
                )
            audit_result = self._fixture_grounding_audit(candidates)
            return audit_result, None, {}
        started_at = utcnow()
        audit_result = await method(payload)
        expected_ids = {
            _required_int(row["candidate_id"], "caption grounding candidate_id")
            for row in candidates
        }
        returned_ids = {assessment.candidate_id for assessment in audit_result.assessments}
        if returned_ids != expected_ids or len(audit_result.assessments) != len(candidates):
            raise AgentRuntimeError(
                "independent caption grounding returned an incomplete or mismatched candidate set"
            )
        usage = dict(self.runtime.last_token_usage)
        model_run_id = self._persist_runtime_model_run(
            channel_id=channel_id,
            candidate_id=candidate_id,
            task_type="verify_caption_grounding",
            prompt_version="caption-grounding-v1",
            request={key: value for key, value in payload.items() if not key.startswith("_")},
            output=audit_result.model_dump(),
            token_usage=usage,
            started_at=started_at,
        )
        return audit_result, model_run_id, usage

    def _persist_runtime_model_run(
        self,
        *,
        channel_id: int,
        candidate_id: int,
        task_type: str,
        prompt_version: str,
        request: dict[str, object],
        output: dict[str, object],
        token_usage: dict[str, object],
        started_at: datetime,
    ) -> int:
        with self.database.session() as session:
            row = ModelRun(
                task_type=task_type,
                provider=self.runtime.provider,
                model=self.runtime.model_name,
                prompt_version=prompt_version,
                input_record_ids_json=json.dumps([candidate_id]),
                request_summary_json=json.dumps(request, sort_keys=True, default=str),
                structured_output_json=json.dumps(output, sort_keys=True, default=str),
                token_usage_json=json.dumps(token_usage, sort_keys=True),
                started_at=started_at,
                completed_at=datetime.now(UTC),
                status="completed",
            )
            session.add(row)
            session.flush()
            model_run_id = row.id
            audit(
                session,
                f"{task_type}_completed",
                "candidate_image",
                candidate_id,
                {
                    "channel_id": channel_id,
                    "model_run_id": model_run_id,
                    "provider": self.runtime.provider,
                    "model": self.runtime.model_name,
                    "prompt_version": prompt_version,
                },
            )
        return model_run_id

    @staticmethod
    def _fixture_grounding_audit(
        candidates: list[dict[str, object]],
    ) -> CaptionGroundingAudit:
        assessments = [
            CaptionGroundingAssessment(
                candidate_id=_required_int(
                    row["candidate_id"],
                    "caption grounding candidate_id",
                ),
                verdict="supported",
                grounding_score=0.95,
                factual_claims=[str(row["text"])],
                supported_claims=[str(row["text"])],
                uncertain_claims=[],
                unsupported_claims=[],
                visual_evidence=["offline fixture evidence"],
                source_evidence=[],
                contradictions=[],
                corrected_caption=None,
                confidence=0.95,
            )
            for row in candidates
        ]
        return CaptionGroundingAudit(
            assessments=assessments,
            image_summary="Offline fixture grounding audit.",
            source_context_used=[],
            audit_confidence=0.95,
        )

    async def _ranked_candidates(
        self,
        *,
        channel_id: int,
        candidate_id: int,
        generated: CaptionCandidateSet,
        brief: EditorialBrief,
        context: dict[str, object],
        analysis: dict[str, Any],
        allowed_reference_ids: list[int],
        image_score: float,
        attempt_number: int,
        prior_candidate_texts: list[str] | None = None,
        image_path: str,
        trusted_source_evidence: dict[str, object],
        detail_image_paths: list[str],
    ) -> tuple[list[dict[str, Any]], int | None, dict[str, object]]:
        with self.database.session() as session:
            existing = [
                value
                for value in session.scalars(
                    select(Post.caption).where(
                        Post.channel_id == channel_id,
                        Post.caption.is_not(None),
                    )
                )
                if value
            ]
            existing.extend(
                value
                for value in session.scalars(
                    select(Proposal.final_caption).where(
                        Proposal.channel_id == channel_id,
                        Proposal.final_caption != "",
                    )
                )
                if value
            )
        pool = list(generated.candidates)
        normalized_prior = {normalize_caption(value) for value in prior_candidate_texts or []}
        seen: set[str] = set(normalized_prior)
        prepared: list[tuple[int, CaptionCandidate, list[str]]] = []
        for generation_index, raw in enumerate(
            pool[: self.generation_configuration["candidate_limit"]],
            start=1,
        ):
            text = raw.text.strip()
            normalized = normalize_caption(text)
            if not normalized:
                continue
            exclusion_reasons: list[str] = []
            if normalized in seen:
                exclusion_reasons.append("duplicate_within_generation")
            seen.add(normalized)
            taxonomy = analyze_caption(text, language=raw.language)
            candidate = raw.model_copy(update={"structure": taxonomy.structure, "text": text})
            if any(
                self._semantic_similarity(channel_id, text, other) >= 0.94 for other in existing
            ):
                exclusion_reasons.append("too_similar_to_existing_caption")
            prepared.append((generation_index, candidate, exclusion_reasons))

        (
            grounding_audit,
            grounding_model_run_id,
            grounding_usage,
        ) = await self._audit_caption_grounding(
            channel_id=channel_id,
            candidate_id=candidate_id,
            image_path=image_path,
            prepared=prepared,
            trusted_source_evidence=trusted_source_evidence,
            detail_image_paths=detail_image_paths,
        )
        grounding_by_id = {
            assessment.candidate_id: assessment for assessment in grounding_audit.assessments
        }

        stats = cast(
            dict[str, Any],
            cast(dict[str, object], context["style_profile"]).get(
                "caption_statistics",
                {},
            ),
        )
        feedback = cast(dict[str, object], context["feedback_context"])
        allowed_feedback_ids = {
            int(item["feedback_signal_id"])
            for collection in ("positive_examples", "negative_examples")
            for item in cast(list[dict[str, Any]], feedback.get(collection, []))
            if isinstance(item, dict) and isinstance(item.get("feedback_signal_id"), int)
        }
        recent_captions = [
            str(item.get("caption") or "")
            for item in cast(
                list[dict[str, Any]],
                context.get("caption_style_examples", []),
            )[:10]
        ]
        rows: list[dict[str, Any]] = []
        for generation_index, candidate, exclusion_reasons in prepared:
            grounding_assessment = normalize_open_question_answer_uncertainty(
                candidate.text,
                grounding_by_id[generation_index],
            )
            verification = self.verifier.verify(candidate, brief)
            claim_verification = self.claim_verifier.verify(
                caption=candidate.text,
                brief=brief,
                first_layer=verification,
                image_path=image_path,
                semantic_assessment=grounding_assessment,
            )
            if not verification.passed:
                exclusion_reasons.append("fast_verification_failed")
            if (
                grounding_assessment.verdict != "supported"
                or grounding_assessment.grounding_score
                < self.generation_configuration["grounding_threshold"]
            ):
                exclusion_reasons.append(f"independent_grounding_{grounding_assessment.verdict}")
            if claim_verification.critical_failures:
                exclusion_reasons.append("critical_claim_failure")
            exclusion_reasons = list(dict.fromkeys(exclusion_reasons))
            effective_grounding = min(
                verification.grounding_score,
                grounding_assessment.grounding_score,
            )
            style = channel_style_score(candidate.text, stats)
            novelty = self._novelty(
                channel_id,
                candidate.text,
                [*existing, *recent_captions],
            )
            rotation = self._novelty(channel_id, candidate.text, recent_captions)
            positive = max(
                0.0,
                self.feedback.positive_similarity(channel_id, candidate.text, feedback),
            )
            negative = max(
                0.0,
                self.feedback.negative_similarity(channel_id, candidate.text, feedback),
            )
            structure_fit = self._structure_fit(candidate.structure, brief.target_structures)
            length_fit = self._length_fit(candidate.text, brief.target_length)
            pairing = self.pair_ranker.score_pair(
                image_score=image_score,
                caption_score=(style + structure_fit + length_fit) / 3,
                grounding_score=effective_grounding,
            )
            components = {
                "grounding": effective_grounding,
                "policy": verification.policy_score,
                "style": style,
                "novelty": novelty,
                "rotation": rotation,
                "positive_feedback": positive,
                "negative_feedback_risk": negative,
                "pairing": pairing.score,
                "structure_fit": structure_fit,
                "length_fit": length_fit,
            }
            preference = self.preference_ranker.score(
                channel_id=channel_id,
                text=candidate.text,
                components=components,
            )
            components["preference"] = preference.score
            generic_penalty = float(
                any(marker in candidate.text.casefold() for marker in GENERIC_PATTERNS)
            )
            weights = cast(
                dict[str, float],
                self.generation_configuration["weights"],
            )
            final = (
                weights["preference"] * preference.score
                + weights["grounding"] * effective_grounding
                + weights["policy"] * verification.policy_score
                + weights["style"] * style
                + weights["novelty"] * novelty
                + weights["rotation"] * rotation
                + weights["positive_feedback"] * positive
                + weights["pairing"] * pairing.score
                + weights["negative_feedback_risk"] * negative
                + weights["generic_penalty"] * generic_penalty
                + 0.08 * structure_fit
                + 0.04 * length_fit
            )
            eligible = verification.passed and claim_verification.passed and not exclusion_reasons
            rows.append(
                {
                    "candidate": candidate,
                    "verification": verification,
                    "claim_verification": claim_verification,
                    "grounding_assessment": grounding_assessment,
                    "components": components,
                    "preference": preference,
                    "pairing": pairing,
                    "generic_penalty": generic_penalty,
                    "final_score": (max(0.0, min(1.0, final)) if eligible else 0.0),
                    "eligible": eligible,
                    "exclusion_reasons": exclusion_reasons,
                    "attempt_number": attempt_number,
                    "generation_index": generation_index,
                    "allowed_reference_ids": [
                        value
                        for value in candidate.historical_evidence
                        if value in set(allowed_reference_ids)
                    ],
                    "allowed_feedback_ids": [
                        value
                        for value in candidate.feedback_evidence
                        if value in allowed_feedback_ids
                    ],
                }
            )
        return self._sort_ranked(rows), grounding_model_run_id, grounding_usage

    @staticmethod
    def _sort_ranked(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        rows.sort(
            key=lambda row: (
                not bool(row["eligible"]),
                -float(row["final_score"]),
                -float(row["components"]["grounding"]),
                int(row["attempt_number"]),
                int(row["generation_index"]),
                str(row["candidate"].text),
            )
        )
        return rows

    @staticmethod
    def _eligible_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [row for row in rows if bool(row["eligible"])]

    def _select_display_slate(
        self,
        ranked: list[dict[str, Any]],
        policy: PolicySnapshot,
        *,
        channel_id: int,
    ) -> list[dict[str, Any]]:
        policy_lead = (
            next(
                (row for row in ranked if row["candidate"].structure == "open_question"),
                ranked[0],
            )
            if policy.question_first
            else ranked[0]
        )
        selected = [policy_lead]
        for target_structure in policy.preferred_structures:
            if len(selected) >= 3:
                break
            if any(row["candidate"].structure == target_structure for row in selected):
                continue
            match = next(
                (
                    row
                    for row in ranked
                    if row not in selected
                    and row["candidate"].structure == target_structure
                    and self._is_diverse(channel_id, row["candidate"].text, selected)
                ),
                None,
            )
            if match is not None:
                selected.append(match)
        for row in ranked:
            if len(selected) >= 3:
                break
            if row not in selected and self._is_diverse(
                channel_id,
                row["candidate"].text,
                selected,
            ):
                selected.append(row)
        if len(selected) < 3:
            for row in ranked:
                if len(selected) >= 3:
                    break
                if row not in selected:
                    selected.append(row)
        return selected[:3]

    def _evidence_provenance(
        self,
        *,
        channel_id: int,
        context: dict[str, object],
        attempts: list[CaptionCandidateSet],
        ranked: list[dict[str, Any]],
    ) -> dict[str, object]:
        retrieval_run_id = _required_int(context["retrieval_run_id"], "retrieval_run_id")
        with self.database.session() as session:
            selected_rows = session.scalars(
                select(RetrievalEvidenceRecord)
                .where(
                    RetrievalEvidenceRecord.retrieval_run_id == retrieval_run_id,
                    RetrievalEvidenceRecord.selected.is_(True),
                )
                .order_by(
                    RetrievalEvidenceRecord.selected_rank,
                    RetrievalEvidenceRecord.id,
                )
            ).all()
        retrieval_selected = [
            {
                "retrieval_evidence_id": row.id,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "role": row.evidence_role,
                "channel": row.retrieval_channel,
                "rank": row.selected_rank,
            }
            for row in selected_rows
        ]
        supplied_post_ids = sorted(
            {
                int(item["post_id"])
                for collection in ("caption_style_examples", "visual_examples")
                for item in cast(list[dict[str, Any]], context.get(collection, []))
                if isinstance(item, dict) and isinstance(item.get("post_id"), int)
            }
        )
        feedback_context = cast(dict[str, object], context.get("feedback_context", {}))
        supplied_feedback_ids = sorted(
            {
                int(item["feedback_signal_id"])
                for collection in ("positive_examples", "negative_examples")
                for item in cast(list[dict[str, Any]], feedback_context.get(collection, []))
                if isinstance(item, dict) and isinstance(item.get("feedback_signal_id"), int)
            }
        )
        supplied_policy_ids = sorted(
            {
                int(item["id"])
                for item in cast(list[dict[str, Any]], context.get("explicit_rules", []))
                if isinstance(item, dict) and isinstance(item.get("id"), int)
            }
        )
        cited_post_ids = sorted(
            {
                value
                for attempt in attempts
                for value in attempt.referenced_historical_post_ids
                if value in set(supplied_post_ids)
            }
            | {
                value
                for row in ranked
                for value in cast(list[int], row.get("allowed_reference_ids", []))
            }
        )
        cited_feedback_ids = sorted(
            {
                value
                for row in ranked
                for value in cast(list[int], row.get("allowed_feedback_ids", []))
            }
        )
        representation_sets = self.representations.snapshot(channel_id)
        return {
            "retrieval_selected": retrieval_selected,
            "model_supplied": {
                "historical_post_ids": supplied_post_ids,
                "feedback_signal_ids": supplied_feedback_ids,
                "policy_rule_ids": supplied_policy_ids,
                "retrieval_evidence_ids": [
                    row["retrieval_evidence_id"] for row in retrieval_selected
                ],
            },
            "model_cited": {
                "historical_post_ids": cited_post_ids,
                "feedback_signal_ids": cited_feedback_ids,
            },
            "ranker_used": {
                "historical_post_ids": supplied_post_ids,
                "feedback_signal_ids": supplied_feedback_ids,
                "style_profile_version": context.get("style_profile_version"),
                "representation_sets": representation_sets,
            },
        }

    def _persist_success(
        self,
        *,
        channel_id: int,
        candidate_id: int,
        brief: EditorialBrief,
        attempts: list[CaptionCandidateSet],
        ranked: list[dict[str, Any]],
        displayed: list[dict[str, Any]],
        context: dict[str, object],
        payload: dict[str, object],
        started_at: datetime,
        latency_ms: float,
        cited_references: list[int],
        agent_run_ids: list[int],
        evidence_provenance: dict[str, object],
        usage: dict[str, object],
    ) -> tuple[CaptionOptions, int]:
        display_texts = [row["candidate"].text for row in displayed]
        confidence = min(
            attempts[-1].confidence,
            sum(float(row["components"]["grounding"]) for row in displayed) / len(displayed),
        )
        rationale = (
            "Runway planned channel-specific angles, verified visible claims, applied explicit "
            "policy, ranked creator preference evidence, and selected a diverse slate. "
            + attempts[-1].rationale.strip()
        )
        with self.database.session() as session:
            model_run = ModelRun(
                task_type="generate_caption_options",
                provider=self.runtime.provider,
                model=self.runtime.model_name,
                prompt_version=self.prompt_version,
                input_record_ids_json=json.dumps(
                    [
                        candidate_id,
                        *cast(
                            list[int],
                            cast(
                                dict[str, object],
                                evidence_provenance["model_supplied"],
                            ).get("historical_post_ids", []),
                        ),
                    ]
                ),
                request_summary_json=json.dumps(payload, sort_keys=True, default=str),
                structured_output_json=json.dumps(
                    {
                        "attempts": [attempt.model_dump() for attempt in attempts],
                        "ranking": [self._public_rank(row) for row in ranked],
                        "displayed": display_texts,
                        "agent_run_ids": agent_run_ids,
                        "evidence_provenance": evidence_provenance,
                    },
                    sort_keys=True,
                ),
                token_usage_json=json.dumps(usage, sort_keys=True),
                started_at=started_at,
                completed_at=datetime.now(UTC),
                status="completed",
            )
            session.add(model_run)
            session.flush()
            slate = CaptionSlate(
                channel_id=channel_id,
                candidate_image_id=candidate_id,
                proposal_id=None,
                retrieval_run_id=_required_int(
                    context["retrieval_run_id"],
                    "retrieval_run_id",
                ),
                model_run_id=model_run.id,
                editorial_brief_json=brief.model_dump_json(),
                raw_output_json=json.dumps(
                    [attempt.model_dump() for attempt in attempts],
                    sort_keys=True,
                ),
                generation_configuration_json=json.dumps(
                    self.generation_configuration,
                    sort_keys=True,
                ),
                configuration_hash=configuration_hash(self.generation_configuration),
                status="completed",
                latency_ms=latency_ms,
                token_usage_json=json.dumps(
                    usage,
                    sort_keys=True,
                ),
                retrieval_selected_evidence_json=json.dumps(
                    evidence_provenance["retrieval_selected"],
                    sort_keys=True,
                ),
                model_supplied_evidence_json=json.dumps(
                    evidence_provenance["model_supplied"],
                    sort_keys=True,
                ),
                model_cited_evidence_json=json.dumps(
                    evidence_provenance["model_cited"],
                    sort_keys=True,
                ),
                ranker_used_evidence_json=json.dumps(
                    evidence_provenance["ranker_used"],
                    sort_keys=True,
                ),
            )
            session.add(slate)
            session.flush()
            display_order = {
                (
                    int(row["attempt_number"]),
                    int(row["generation_index"]),
                ): index
                for index, row in enumerate(displayed, start=1)
            }
            self._persist_candidate_records(
                session=session,
                slate_id=slate.id,
                channel_id=channel_id,
                ranked=ranked,
                display_order=display_order,
                evidence_provenance=evidence_provenance,
            )
            audit(
                session,
                "captions_generated",
                "candidate_image",
                candidate_id,
                {
                    "prompt_version": self.prompt_version,
                    "retrieval_run_id": context["retrieval_run_id"],
                    "caption_slate_id": slate.id,
                    "candidate_count": len(ranked),
                    "displayed_count": len(displayed),
                    "policy_version": brief.policy_version,
                    "configuration_hash": slate.configuration_hash,
                    "agent_run_ids": agent_run_ids,
                },
            )
            slate_id = slate.id
        try:
            JointMultimodalReranker(
                self.database,
                self.settings,
                self.representations,
            ).shadow_evaluate(slate_id)
        except Exception as exc:
            with self.database.session() as session:
                persisted = session.get(CaptionSlate, slate_id)
                if persisted is not None:
                    persisted.reranker_run_json = json.dumps(
                        {
                            "status": "shadow_failed",
                            "activated": False,
                            "error": f"{type(exc).__name__}: {exc}"[:1000],
                        },
                        sort_keys=True,
                    )
                audit(
                    session,
                    "joint_reranker_shadow_failed",
                    "caption_slate",
                    slate_id,
                    {"error": f"{type(exc).__name__}: {exc}"[:1000]},
                )
        return (
            CaptionOptions(
                recommended=display_texts[0],
                alternatives=display_texts[1:3],
                rationale=rationale,
                confidence=max(0.0, min(1.0, confidence)),
                referenced_historical_post_ids=cited_references,
                factual_uncertainty_warning=attempts[-1].factual_uncertainty_warning,
                slate_id=slate_id,
                retrieval_run_id=_required_int(
                    context["retrieval_run_id"],
                    "retrieval_run_id",
                ),
            ),
            slate_id,
        )

    def _persist_abstention(
        self,
        *,
        channel_id: int,
        candidate_id: int,
        brief: EditorialBrief,
        attempts: list[CaptionCandidateSet],
        ranked: list[dict[str, Any]],
        context: dict[str, object],
        payload: dict[str, object],
        started_at: datetime,
        latency_ms: float,
        reason: str,
        agent_run_ids: list[int],
        evidence_provenance: dict[str, object],
        usage: dict[str, object],
    ) -> int:
        with self.database.session() as session:
            model_run = ModelRun(
                task_type="generate_caption_options",
                provider=self.runtime.provider,
                model=self.runtime.model_name,
                prompt_version=self.prompt_version,
                input_record_ids_json=json.dumps([candidate_id]),
                request_summary_json=json.dumps(payload, sort_keys=True, default=str),
                structured_output_json=json.dumps(
                    {
                        "attempts": [attempt.model_dump() for attempt in attempts],
                        "abstention": reason,
                        "agent_run_ids": agent_run_ids,
                        "evidence_provenance": evidence_provenance,
                    },
                    sort_keys=True,
                ),
                token_usage_json=json.dumps(usage, sort_keys=True),
                started_at=started_at,
                completed_at=datetime.now(UTC),
                status="completed",
            )
            session.add(model_run)
            session.flush()
            slate = CaptionSlate(
                channel_id=channel_id,
                candidate_image_id=candidate_id,
                retrieval_run_id=_required_int(
                    context["retrieval_run_id"],
                    "retrieval_run_id",
                ),
                model_run_id=model_run.id,
                editorial_brief_json=brief.model_dump_json(),
                raw_output_json=json.dumps(
                    [attempt.model_dump() for attempt in attempts],
                    sort_keys=True,
                ),
                generation_configuration_json=json.dumps(
                    self.generation_configuration,
                    sort_keys=True,
                ),
                configuration_hash=configuration_hash(self.generation_configuration),
                status="abstained",
                latency_ms=latency_ms,
                token_usage_json=json.dumps(
                    usage,
                    sort_keys=True,
                ),
                failure_summary=reason,
                retrieval_selected_evidence_json=json.dumps(
                    evidence_provenance["retrieval_selected"],
                    sort_keys=True,
                ),
                model_supplied_evidence_json=json.dumps(
                    evidence_provenance["model_supplied"],
                    sort_keys=True,
                ),
                model_cited_evidence_json=json.dumps(
                    evidence_provenance["model_cited"],
                    sort_keys=True,
                ),
                ranker_used_evidence_json=json.dumps(
                    evidence_provenance["ranker_used"],
                    sort_keys=True,
                ),
            )
            session.add(slate)
            session.flush()
            self._persist_candidate_records(
                session=session,
                slate_id=slate.id,
                channel_id=channel_id,
                ranked=ranked,
                display_order={},
                evidence_provenance=evidence_provenance,
            )
            audit(
                session,
                "caption_generation_abstained",
                "candidate_image",
                candidate_id,
                {
                    "caption_slate_id": slate.id,
                    "reason": reason,
                    "retrieval_run_id": context["retrieval_run_id"],
                    "agent_run_ids": agent_run_ids,
                },
            )
            return slate.id

    def _persist_candidate_records(
        self,
        *,
        session: Any,
        slate_id: int,
        channel_id: int,
        ranked: list[dict[str, Any]],
        display_order: dict[tuple[int, int], int],
        evidence_provenance: dict[str, object],
    ) -> None:
        for rank, row in enumerate(ranked, start=1):
            candidate = cast(CaptionCandidate, row["candidate"])
            verification = cast(VerificationResult, row["verification"])
            components = cast(dict[str, float], row["components"])
            preference = cast(PreferenceScore, row["preference"])
            exclusion_reasons = cast(list[str], row["exclusion_reasons"])
            candidate_key = (
                int(row["attempt_number"]),
                int(row["generation_index"]),
            )
            snapshot_json, snapshot_hash = snapshot_json_and_hash(
                candidate.text,
                components,
                context={
                    "origin": "generated",
                    "attempt_number": candidate_key[0],
                    "generation_index": candidate_key[1],
                    "eligible": bool(row["eligible"]),
                },
            )
            record = CaptionCandidateRecord(
                channel_id=channel_id,
                caption_slate_id=slate_id,
                text=candidate.text,
                language=candidate.language,
                structure=candidate.structure,
                editorial_angle=candidate.editorial_angle,
                visible_evidence_json=json.dumps(candidate.visible_evidence),
                uncertainty_json=json.dumps(candidate.uncertainty),
                prohibited_claim_checks_json=json.dumps(
                    {
                        "checks": verification.checks,
                        "eligible": bool(row["eligible"]),
                        "exclusion_reasons": exclusion_reasons,
                    },
                    sort_keys=True,
                ),
                historical_evidence_json=json.dumps(row["allowed_reference_ids"]),
                feedback_evidence_json=json.dumps(row["allowed_feedback_ids"]),
                generator_confidence=candidate.confidence,
                verifier_result_json=verification.model_dump_json(),
                eligible=bool(row["eligible"]),
                exclusion_reasons_json=json.dumps(exclusion_reasons),
                attempt_number=int(row["attempt_number"]),
                generation_index=int(row["generation_index"]),
                grounding_score=components["grounding"],
                policy_score=verification.policy_score,
                style_score=components["style"],
                novelty_score=components["novelty"],
                rotation_score=components["rotation"],
                positive_feedback_score=components["positive_feedback"],
                negative_feedback_risk=components["negative_feedback_risk"],
                pairing_score=components["pairing"],
                preference_score=components["preference"],
                final_score=float(row["final_score"]),
                rank=rank,
                displayed=candidate_key in display_order,
                display_order=display_order.get(candidate_key),
                origin="generated",
                created_by="intelligence_agent",
                feature_schema_version=FEATURE_SCHEMA_VERSION,
                feature_snapshot_json=snapshot_json,
                feature_snapshot_hash=snapshot_hash,
                taxonomy_version=TAXONOMY_VERSION,
                verifier_version=VERIFIER_VERSION,
                ranker_model_version_id=preference.model_version_id,
                model_supplied_evidence_json=json.dumps(
                    evidence_provenance["model_supplied"],
                    sort_keys=True,
                ),
                model_cited_evidence_json=json.dumps(
                    {
                        "historical_post_ids": row["allowed_reference_ids"],
                        "feedback_signal_ids": row["allowed_feedback_ids"],
                    },
                    sort_keys=True,
                ),
                ranker_used_evidence_json=json.dumps(
                    evidence_provenance["ranker_used"],
                    sort_keys=True,
                ),
                claim_verification_json=json.dumps(
                    row["claim_verification"].as_dict(),
                    sort_keys=True,
                ),
            )
            session.add(record)
            session.flush()
            raw_provider, resolution = self.representations.resolve(
                channel_id,
                modality="text",
            )
            provider = cast(TextEmbeddingProvider, raw_provider)
            representation = RepresentationStore.persist_in_session(
                session,
                channel_id=channel_id,
                entity_type="caption_candidate",
                entity_id=record.id,
                field="text",
                modality="text",
                result=provider.embed_text(
                    candidate.text,
                    purpose="caption_candidate_semantics",
                ),
                source_content_hash=content_hash(candidate.text),
                metadata={
                    "caption_slate_id": slate_id,
                    "origin": "generated",
                    "representation_resolution": resolution.as_dict(),
                },
            )
            record.representation_record_id = representation.id

    @staticmethod
    def _public_rank(row: dict[str, Any]) -> dict[str, object]:
        candidate = cast(CaptionCandidate, row["candidate"])
        verification = cast(VerificationResult, row["verification"])
        preference = row["preference"]
        pairing = row["pairing"]
        return {
            "text": candidate.text,
            "structure": candidate.structure,
            "editorial_angle": candidate.editorial_angle,
            "visible_evidence": candidate.visible_evidence,
            "uncertainty": candidate.uncertainty,
            "generator_confidence": candidate.confidence,
            "verification": verification.model_dump(),
            "independent_grounding": row["grounding_assessment"].model_dump(),
            "claim_verification": row["claim_verification"].as_dict(),
            "components": row["components"],
            "preference": {
                "score": preference.score,
                "trained": preference.trained,
                "calibrated": preference.calibrated,
                "reason": preference.reason,
                "sample_count": preference.sample_count,
                "model_version_id": preference.model_version_id,
            },
            "pairing": {
                "score": pairing.score,
                "reason": pairing.reason,
            },
            "generic_penalty": row["generic_penalty"],
            "eligible": bool(row["eligible"]),
            "exclusion_reasons": row["exclusion_reasons"],
            "attempt_number": row["attempt_number"],
            "generation_index": row["generation_index"],
            "final_score": round(float(row["final_score"]), 6),
        }

    def _is_diverse(
        self,
        channel_id: int,
        text: str,
        selected: list[dict[str, Any]],
    ) -> bool:
        threshold = float(self.generation_configuration["diversity_similarity_threshold"])
        return all(
            self._semantic_similarity(
                channel_id,
                text,
                cast(CaptionCandidate, row["candidate"]).text,
            )
            < threshold
            for row in selected
        )

    def _semantic_similarity(self, channel_id: int, first: str, second: str) -> float:
        return self.representations.text_similarity(
            channel_id,
            first,
            second,
            score_purpose="caption_semantics",
        ).score

    def _novelty(self, channel_id: int, text: str, comparison: list[str]) -> float:
        highest = max(
            (
                max(0.0, self._semantic_similarity(channel_id, text, other))
                for other in comparison
                if other.strip()
            ),
            default=0.0,
        )
        return max(0.0, min(1.0, 1.0 - highest))

    @staticmethod
    def _structure_fit(structure: str, targets: list[str]) -> float:
        try:
            index = targets.index(structure)
        except ValueError:
            return 0.35
        return max(0.4, 1.0 - 0.18 * index)

    @staticmethod
    def _length_fit(text: str, target: dict[str, int]) -> float:
        count = len(caption_features(text)["tokens"])
        minimum = target["minimum_words"]
        maximum = target["maximum_words"]
        if minimum <= count <= maximum:
            return 1.0
        distance = min(abs(count - minimum), abs(count - maximum))
        return math.exp(-distance / max((minimum + maximum) / 2, 1))

    @staticmethod
    def _json_dict(value: str) -> dict[str, Any]:
        try:
            result = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return {}
        return result if isinstance(result, dict) else {}
