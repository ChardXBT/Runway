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
from runway.analysis.runtime import AgentRuntime, runtime_for
from runway.analysis.schemas import CaptionCandidate, CaptionCandidateSet, CaptionOptions
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
    SearchRun,
    utcnow,
)
from runway.db.repositories import audit, get_channel
from runway.intelligence.agent_harness import (
    AgentBudget,
    AgentInputEnvelope,
    AgentOutputEnvelope,
    AgentStepResult,
    IntelligenceAgentHarness,
)
from runway.intelligence.embeddings import (
    DeterministicTextEmbeddingProvider,
    configuration_hash,
    cosine,
)
from runway.intelligence.policies import PolicySnapshot
from runway.intelligence.retrieval import RetrievalService

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


class CaptionService:
    prompt_version = "captions-v4"
    generation_configuration: dict[str, Any] = {
        "version": "canonical-caption-pipeline-1",
        "candidate_limit": 12,
        "display_limit": 3,
        "retry_limit": 1,
        "grounding_threshold": 0.72,
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
        self.preference_ranker = PairwiseCaptionPreferenceRanker(database)
        self.pair_ranker = DeterministicImageCaptionPairRanker()
        self.text_provider = DeterministicTextEmbeddingProvider()
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
            analysis = self._json_dict(candidate.detected_topic_json)
            source_context: dict[str, object] = {
                "candidate_id": candidate.id,
                "media_asset_id": media.id,
                "source_page_url": candidate.source_page_url,
                "source_domain": candidate.source_domain,
                "rights_status": candidate.rights_status,
            }
            image_score = candidate.final_rank_score

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
        )
        caption_examples = cast(
            list[dict[str, Any]],
            context["caption_style_examples"],
        )
        historical_ids = [int(item["post_id"]) for item in caption_examples]
        payload: dict[str, object] = {
            "candidate_id": candidate_id,
            "candidate_analysis": analysis,
            "historical_post_ids": historical_ids,
            "retrieval_context": context,
            "editorial_brief": brief.model_dump(),
            "_image_path": str(image_path),
        }
        started_at = utcnow()
        started_perf = time.perf_counter()
        attempts: list[CaptionCandidateSet] = []
        agent_run_ids: list[int] = []
        generated, agent_run_id = await self._generate_with_agent(
            channel_id=channel_id,
            candidate_id=candidate_id,
            payload=payload,
        )
        agent_run_ids.append(agent_run_id)
        attempts.append(generated)
        ranked = self._ranked_candidates(
            channel_id=channel_id,
            candidate_id=candidate_id,
            generated=generated,
            brief=brief,
            context=context,
            analysis=analysis,
            allowed_reference_ids=historical_ids,
            image_score=image_score,
            attempt_number=1,
        )
        if len(self._eligible_rows(ranked)) < 3:
            retry_payload = {
                **payload,
                "retry": {
                    "reason": "fewer than three grounded, policy-compliant candidates survived",
                    "failed_candidates": [
                        {
                            "text": row["candidate"].text,
                            "verifier": row["verification"].model_dump(),
                        }
                        for row in ranked
                        if not row["eligible"]
                    ],
                    "instruction": (
                        "Use only the brief's highest-confidence visible facts and vary structure."
                    ),
                },
            }
            generated_retry, retry_agent_run_id = await self._generate_with_agent(
                channel_id=channel_id,
                candidate_id=candidate_id,
                payload=retry_payload,
            )
            agent_run_ids.append(retry_agent_run_id)
            attempts.append(generated_retry)
            retry_ranked = self._ranked_candidates(
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
            )
            ranked.extend(retry_ranked)
            ranked = self._sort_ranked(ranked)

        latency_ms = round((time.perf_counter() - started_perf) * 1000, 3)
        eligible = self._eligible_rows(ranked)
        if len(eligible) < 3:
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
            )
            for run_id in agent_run_ids:
                self.agent_harness.link_artifact(
                    run_id,
                    artifact_type="caption_slate",
                    artifact_id=slate_id,
                )
            return CaptionOptions(
                recommended="",
                alternatives=[],
                rationale="RunWay abstained instead of displaying generic filler.",
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

        displayed = self._select_display_slate(eligible, policy)
        supplied_references = [
            value
            for value in attempts[-1].referenced_historical_post_ids
            if value in set(historical_ids)
        ]
        references = supplied_references or historical_ids[:4]
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
            references=references,
            agent_run_ids=agent_run_ids,
        )
        for run_id in agent_run_ids:
            self.agent_harness.link_artifact(
                run_id,
                artifact_type="caption_slate",
                artifact_id=slate_id,
            )
        result.slate_id = slate_id
        return result

    async def _generate_with_agent(
        self,
        *,
        channel_id: int,
        candidate_id: int,
        payload: dict[str, object],
    ) -> tuple[CaptionCandidateSet, int]:
        async def handler(
            envelope: AgentInputEnvelope,
            _attempt: int,
        ) -> AgentStepResult:
            generated = await self.runtime.generate_caption_options(envelope.payload)
            return AgentStepResult(
                output=AgentOutputEnvelope(payload=generated.model_dump()),
                usage=dict(self.runtime.last_token_usage),
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
        return CaptionCandidateSet.model_validate(output.payload), run_id

    def _ranked_candidates(
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
    ) -> list[dict[str, Any]]:
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
            if any(self._semantic_similarity(text, other) >= 0.94 for other in existing):
                exclusion_reasons.append("too_similar_to_existing_caption")
            prepared.append((generation_index, candidate, exclusion_reasons))

        stats = cast(
            dict[str, Any],
            cast(dict[str, object], context["style_profile"]).get(
                "caption_statistics",
                {},
            ),
        )
        feedback = cast(dict[str, object], context["feedback_context"])
        recent_captions = [
            str(item.get("caption") or "")
            for item in cast(
                list[dict[str, Any]],
                context.get("caption_style_examples", []),
            )[:10]
        ]
        rows: list[dict[str, Any]] = []
        for generation_index, candidate, exclusion_reasons in prepared:
            verification = self.verifier.verify(candidate, brief)
            style = channel_style_score(candidate.text, stats)
            novelty = self._novelty(candidate.text, [*existing, *recent_captions])
            rotation = self._novelty(candidate.text, recent_captions)
            positive = max(
                0.0,
                self.feedback.positive_similarity(candidate.text, feedback),
            )
            negative = max(
                0.0,
                self.feedback.negative_similarity(candidate.text, feedback),
            )
            structure_fit = self._structure_fit(candidate.structure, brief.target_structures)
            length_fit = self._length_fit(candidate.text, brief.target_length)
            pairing = self.pair_ranker.score_pair(
                image_score=image_score,
                caption_score=(style + structure_fit + length_fit) / 3,
                grounding_score=verification.grounding_score,
            )
            components = {
                "grounding": verification.grounding_score,
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
                + weights["grounding"] * verification.grounding_score
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
            eligible = verification.passed and not exclusion_reasons
            rows.append(
                {
                    "candidate": candidate,
                    "verification": verification,
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
                }
            )
        return self._sort_ranked(rows)

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
                    and self._is_diverse(row["candidate"].text, selected)
                ),
                None,
            )
            if match is not None:
                selected.append(match)
        for row in ranked:
            if len(selected) >= 3:
                break
            if row not in selected and self._is_diverse(row["candidate"].text, selected):
                selected.append(row)
        if len(selected) < 3:
            for row in ranked:
                if len(selected) >= 3:
                    break
                if row not in selected:
                    selected.append(row)
        return selected[:3]

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
        references: list[int],
        agent_run_ids: list[int],
    ) -> tuple[CaptionOptions, int]:
        display_texts = [row["candidate"].text for row in displayed]
        confidence = min(
            attempts[-1].confidence,
            sum(float(row["verification"].grounding_score) for row in displayed) / len(displayed),
        )
        rationale = (
            "RunWay planned channel-specific angles, verified visible claims, applied explicit "
            "policy, ranked creator preference evidence, and selected a diverse slate. "
            + attempts[-1].rationale.strip()
        )
        with self.database.session() as session:
            model_run = ModelRun(
                task_type="generate_caption_options",
                provider=self.runtime.provider,
                model=self.runtime.model_name,
                prompt_version=self.prompt_version,
                input_record_ids_json=json.dumps([candidate_id, *references]),
                request_summary_json=json.dumps(payload, sort_keys=True, default=str),
                structured_output_json=json.dumps(
                    {
                        "attempts": [attempt.model_dump() for attempt in attempts],
                        "ranking": [self._public_rank(row) for row in ranked],
                        "displayed": display_texts,
                        "agent_run_ids": agent_run_ids,
                    },
                    sort_keys=True,
                ),
                token_usage_json=json.dumps(self.runtime.last_token_usage, sort_keys=True),
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
                    self.runtime.last_token_usage,
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
        return (
            CaptionOptions(
                recommended=display_texts[0],
                alternatives=display_texts[1:3],
                rationale=rationale,
                confidence=max(0.0, min(1.0, confidence)),
                referenced_historical_post_ids=references,
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
                    },
                    sort_keys=True,
                ),
                token_usage_json=json.dumps(self.runtime.last_token_usage, sort_keys=True),
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
                    self.runtime.last_token_usage,
                    sort_keys=True,
                ),
                failure_summary=reason,
            )
            session.add(slate)
            session.flush()
            self._persist_candidate_records(
                session=session,
                slate_id=slate.id,
                channel_id=channel_id,
                ranked=ranked,
                display_order={},
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

    @staticmethod
    def _persist_candidate_records(
        *,
        session: Any,
        slate_id: int,
        channel_id: int,
        ranked: list[dict[str, Any]],
        display_order: dict[tuple[int, int], int],
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
            session.add(
                CaptionCandidateRecord(
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
                    feedback_evidence_json=json.dumps(candidate.feedback_evidence),
                    generator_confidence=candidate.confidence,
                    verifier_result_json=verification.model_dump_json(),
                    eligible=bool(row["eligible"]),
                    exclusion_reasons_json=json.dumps(exclusion_reasons),
                    attempt_number=int(row["attempt_number"]),
                    generation_index=int(row["generation_index"]),
                    grounding_score=verification.grounding_score,
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
                )
            )

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
        text: str,
        selected: list[dict[str, Any]],
    ) -> bool:
        threshold = float(self.generation_configuration["diversity_similarity_threshold"])
        return all(
            self._semantic_similarity(
                text,
                cast(CaptionCandidate, row["candidate"]).text,
            )
            < threshold
            for row in selected
        )

    def _semantic_similarity(self, first: str, second: str) -> float:
        first_vector = self.text_provider.embed_text(
            first,
            purpose="caption_semantics",
        ).as_array()[0]
        second_vector = self.text_provider.embed_text(
            second,
            purpose="caption_semantics",
        ).as_array()[0]
        return cosine(first_vector, second_vector)

    def _novelty(self, text: str, comparison: list[str]) -> float:
        highest = max(
            (
                max(0.0, self._semantic_similarity(text, other))
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
