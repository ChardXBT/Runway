from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import cast

from sqlalchemy import select

from runway.analysis.runtime import AgentRuntime, runtime_for
from runway.analysis.schemas import ShadowEditorialRecommendation
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CandidateImage,
    FeedbackSignal,
    MediaAsset,
    Proposal,
    ShadowEditorialDecision,
    StyleProfile,
)
from runway.db.repositories import get_channel
from runway.intelligence.agent_harness import (
    AgentBudget,
    AgentInputEnvelope,
    AgentOutputEnvelope,
    AgentStepResult,
    IntelligenceAgentHarness,
)


class ShadowEditorialService:
    """Advisory creator simulator whose output is isolated from human learning labels."""

    evaluator_version = "shadow-eric-v1"

    def __init__(
        self,
        database: Database,
        settings: Settings,
        runtime: AgentRuntime | None = None,
    ):
        self.database = database
        self.settings = settings
        self.runtime = runtime or runtime_for(settings)
        self.harness = IntelligenceAgentHarness(database)

    async def review_pending(self, *, limit: int = 50) -> dict[str, object]:
        if limit < 1 or limit > 500:
            raise ValueError("shadow review limit must be between 1 and 500")
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            proposal_ids = session.scalars(
                select(Proposal.id)
                .where(
                    Proposal.channel_id == channel_id,
                    Proposal.status == "needs_review",
                    Proposal.id.not_in(
                        select(ShadowEditorialDecision.proposal_id).where(
                            ShadowEditorialDecision.evaluator_version == self.evaluator_version
                        )
                    ),
                )
                .order_by(Proposal.created_at, Proposal.id)
                .limit(limit)
            ).all()
        created: list[int] = []
        for proposal_id in proposal_ids:
            created.append(await self.review_one(int(proposal_id)))
        report = self.report()
        report["created_decision_ids"] = created
        return report

    async def review_one(self, proposal_id: int) -> int:
        payload, channel_id, candidate_id, cluster_key, confidence_cap = self._payload(proposal_id)

        async def handler(
            envelope: AgentInputEnvelope,
            _attempt: int,
        ) -> AgentStepResult:
            recommendation = await self.runtime.simulate_editorial_decision(envelope.payload)
            return AgentStepResult(
                output=AgentOutputEnvelope(payload=recommendation.model_dump()),
                usage=self.runtime.last_token_usage,
            )

        run_id, output = await self.harness.execute(
            channel_id=channel_id,
            capability="shadow_editorial_simulation",
            provider=self.runtime.provider,
            model=self.runtime.model_name,
            prompt_version=self.evaluator_version,
            input_value=AgentInputEnvelope(
                entity_ids=[proposal_id, candidate_id],
                payload=payload,
            ),
            handler=handler,
            budget=AgentBudget(
                max_steps=1,
                max_attempts_per_step=1,
                max_total_tokens=20_000,
                max_seconds=float(self.settings.codex_timeout_seconds),
                timeout_seconds=float(self.settings.codex_timeout_seconds),
            ),
            run_key=f"{self.evaluator_version}:proposal:{proposal_id}",
        )
        recommendation = ShadowEditorialRecommendation.model_validate(output.payload)
        calibrated = recommendation.model_copy(
            update={"confidence": min(recommendation.confidence, confidence_cap)}
        )
        public_snapshot = {key: value for key, value in payload.items() if not key.startswith("_")}
        with self.database.session() as session:
            existing = session.scalar(
                select(ShadowEditorialDecision).where(
                    ShadowEditorialDecision.proposal_id == proposal_id,
                    ShadowEditorialDecision.evaluator_version == self.evaluator_version,
                )
            )
            if existing is not None:
                return existing.id
            record = ShadowEditorialDecision(
                channel_id=channel_id,
                proposal_id=proposal_id,
                candidate_image_id=candidate_id,
                agent_run_id=run_id,
                evaluator_version=self.evaluator_version,
                label_source="synthetic",
                training_eligible=False,
                decision=calibrated.decision,
                edited_caption=calibrated.edited_caption,
                image_score=calibrated.image_score,
                caption_score=calibrated.caption_score,
                pairing_score=calibrated.pairing_score,
                confidence=calibrated.confidence,
                reason_codes_json=json.dumps(calibrated.reason_codes, sort_keys=True),
                rationale=calibrated.rationale,
                diversity_cluster_key=cluster_key,
                input_snapshot_json=json.dumps(public_snapshot, sort_keys=True, default=str),
            )
            session.add(record)
            session.flush()
            decision_id = record.id
        self.harness.link_artifact(
            run_id,
            artifact_type="shadow_editorial_decision",
            artifact_id=decision_id,
        )
        return decision_id

    def report(self) -> dict[str, object]:
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            rows = session.scalars(
                select(ShadowEditorialDecision)
                .where(ShadowEditorialDecision.channel_id == channel_id)
                .order_by(ShadowEditorialDecision.created_at, ShadowEditorialDecision.id)
            ).all()
        decisions = Counter(row.decision for row in rows)
        clusters = Counter(row.diversity_cluster_key or "unclustered" for row in rows)
        return {
            "evaluator_version": self.evaluator_version,
            "decision_count": len(rows),
            "decisions": dict(sorted(decisions.items())),
            "unique_clusters": len(clusters),
            "largest_cluster_size": max(clusters.values(), default=0),
            "training_eligible_count": sum(row.training_eligible for row in rows),
            "label_sources": sorted({row.label_source for row in rows}),
            "safety": {
                "mutates_proposals": False,
                "writes_creator_feedback": False,
                "can_schedule_or_publish": False,
            },
        }

    def _payload(
        self,
        proposal_id: int,
    ) -> tuple[dict[str, object], int, int, str | None, float]:
        with self.database.session() as session:
            channel_id = get_channel(session, self.settings.channel_handle).id
            proposal = session.scalar(
                select(Proposal).where(
                    Proposal.id == proposal_id,
                    Proposal.channel_id == channel_id,
                )
            )
            if proposal is None:
                raise LookupError(f"proposal {proposal_id} not found")
            candidate = session.get(CandidateImage, proposal.candidate_image_id)
            if candidate is None:
                raise LookupError("proposal candidate is missing")
            media = session.get(MediaAsset, candidate.media_asset_id)
            if media is None:
                raise LookupError("proposal media is missing")
            profile = session.scalar(
                select(StyleProfile)
                .where(
                    StyleProfile.channel_id == channel_id,
                    StyleProfile.is_active.is_(True),
                )
                .order_by(StyleProfile.version.desc())
                .limit(1)
            )
            feedback = session.scalars(
                select(FeedbackSignal)
                .where(
                    FeedbackSignal.channel_id == channel_id,
                    FeedbackSignal.source == "creator",
                )
                .order_by(FeedbackSignal.created_at.desc(), FeedbackSignal.id.desc())
                .limit(40)
            ).all()
        feedback_examples = [
            {
                "target": row.target,
                "verdict": row.verdict,
                "value_text": row.value_text,
                "reason_codes": json.loads(row.reason_codes_json),
            }
            for row in feedback
        ]
        feedback_count = len(feedback_examples)
        confidence_cap = min(0.85, 0.4 + feedback_count * 0.015)
        profile_payload: dict[str, object] = {}
        if profile is not None:
            raw_profile = json.loads(profile.profile_json)
            profile_payload = cast(dict[str, object], raw_profile)
        payload: dict[str, object] = {
            "proposal_id": proposal.id,
            "final_caption": proposal.final_caption,
            "recommended_caption": proposal.recommended_caption,
            "alternative_captions": json.loads(proposal.alternative_captions_json),
            "candidate": {
                "id": candidate.id,
                "analysis": json.loads(candidate.detected_topic_json),
                "final_rank_score": candidate.final_rank_score,
                "diversity_cluster_key": candidate.diversity_cluster_key,
                "diversity_fingerprint": json.loads(candidate.diversity_fingerprint_json or "{}"),
            },
            "creator_feedback_sample_count": feedback_count,
            "creator_feedback": feedback_examples,
            "channel_profile": profile_payload,
            "_image_path": str(
                (self.settings.resolved_data_dir / Path(media.local_path)).resolve()
            ),
        }
        return payload, channel_id, candidate.id, candidate.diversity_cluster_key, confidence_cap
