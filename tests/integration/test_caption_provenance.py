from __future__ import annotations

import json
from collections.abc import Mapping

import pytest
from sqlalchemy import select

from runway.analysis.runtime import MockAgentRuntime
from runway.analysis.schemas import (
    CaptionCandidate,
    CaptionCandidateSet,
    CaptionGroundingAssessment,
    CaptionGroundingAudit,
)
from runway.analysis.service import AnalysisService
from runway.captions.service import CaptionService
from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CaptionCandidateRecord,
    CaptionSlate,
    IntelligenceAgentRun,
    IntelligenceAgentStep,
    ModelRun,
    MultimodalRerankRun,
    PairwisePreference,
)
from runway.discovery.service import DiscoveryService
from runway.intelligence.hard_negatives import HardNegativeMiningService
from runway.intelligence.profile import StyleProfileService


class UngroundedCaptionRuntime(MockAgentRuntime):
    async def generate_caption_options(
        self,
        payload: object,
    ) -> CaptionCandidateSet:
        del payload
        candidates = [
            CaptionCandidate(
                text="Why did Bart just win the episode?",
                structure="open_question",
                language="en",
                editorial_angle="invented_plot",
                visible_evidence=["Bart"],
                uncertainty=[],
                historical_evidence=[],
                feedback_evidence=[],
                confidence=0.9,
            ),
            CaptionCandidate(
                text='Why did Bart say "I won"?',
                structure="open_question",
                language="en",
                editorial_angle="invented_quote",
                visible_evidence=["Bart"],
                uncertainty=[],
                historical_evidence=[],
                feedback_evidence=[],
                confidence=0.9,
            ),
            CaptionCandidate(
                text="Bart is Homer's brother.",
                structure="observation",
                language="en",
                editorial_angle="invented_relationship",
                visible_evidence=["Bart"],
                uncertainty=[],
                historical_evidence=[],
                feedback_evidence=[],
                confidence=0.9,
            ),
        ]
        return CaptionCandidateSet(
            candidates=candidates,
            rationale="Deliberately invalid grounding fixture.",
            confidence=0.9,
            referenced_historical_post_ids=[],
            factual_uncertainty_warning=None,
        )


class FalsePremiseCaptionRuntime(MockAgentRuntime):
    """A generator that mixes one plausible hallucination into a valid slate."""

    async def generate_caption_options(
        self,
        payload: Mapping[str, object],
    ) -> CaptionCandidateSet:
        del payload
        texts = [
            "What snack is hidden under the blanket?",
            "What detail stands out first?",
            "How would you caption this moment?",
            "Which detail caught your eye?",
            "What happens next?",
            "What would you call this scene?",
        ]
        return CaptionCandidateSet(
            candidates=[
                CaptionCandidate(
                    text=text,
                    structure="open_question",
                    language="en",
                    editorial_angle="audience_inquiry",
                    visible_evidence=[],
                    uncertainty=[],
                    historical_evidence=[],
                    feedback_evidence=[],
                    confidence=0.9,
                )
                for text in texts
            ],
            rationale="Adversarial slate with one visually unsupported premise.",
            confidence=0.9,
            referenced_historical_post_ids=[],
            factual_uncertainty_warning=None,
        )

    async def verify_caption_grounding(
        self,
        payload: Mapping[str, object],
    ) -> CaptionGroundingAudit:
        rows = payload.get("candidates", [])
        candidates = rows if isinstance(rows, list) else []
        assessments: list[CaptionGroundingAssessment] = []
        for row in candidates:
            if not isinstance(row, Mapping):
                continue
            candidate_id = int(row["candidate_id"])
            text = str(row["text"])
            false_premise = "hidden under the blanket" in text.casefold()
            assessments.append(
                CaptionGroundingAssessment(
                    candidate_id=candidate_id,
                    verdict="unsupported" if false_premise else "supported",
                    grounding_score=0.08 if false_premise else 0.96,
                    factual_claims=[text],
                    supported_claims=[] if false_premise else [text],
                    uncertain_claims=[],
                    unsupported_claims=["a snack is hidden under the blanket"]
                    if false_premise
                    else [],
                    visual_evidence=["no hidden snack is visible"]
                    if false_premise
                    else ["the open question adds no unsupported premise"],
                    source_evidence=[],
                    contradictions=[],
                    corrected_caption="What is happening in this scene?" if false_premise else None,
                    confidence=0.97,
                )
            )
        return CaptionGroundingAudit(
            assessments=assessments,
            image_summary="Independent adversarial fixture audit.",
            source_context_used=[],
            audit_confidence=0.97,
        )


@pytest.mark.asyncio
async def test_complete_caption_slate_and_abstention_provenance_are_persisted(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    await AnalysisService(database, settings).analyze_history()
    await StyleProfileService(database, settings).build()
    discovery = DiscoveryService(database, settings)
    result = await discovery.discover(provider_name="fixture", dry_run=True)
    candidate_id = int(
        discovery.list_candidates(
            run_id=int(result["run_id"]),
            accepted_only=True,
        )[0]["id"]
    )

    completed = await CaptionService(database, settings).generate(candidate_id)
    assert completed.abstained is False
    with database.session() as session:
        completed_slate = session.get(CaptionSlate, completed.slate_id)
        assert completed_slate is not None
        completed_records = session.scalars(
            select(CaptionCandidateRecord)
            .where(CaptionCandidateRecord.caption_slate_id == completed_slate.id)
            .order_by(CaptionCandidateRecord.rank)
        ).all()
        raw_attempts = json.loads(completed_slate.raw_output_json)
        editorial_brief = json.loads(completed_slate.editorial_brief_json)
        supplied = json.loads(completed_slate.model_supplied_evidence_json)
        cited = json.loads(completed_slate.model_cited_evidence_json)
        reranker = session.scalar(
            select(MultimodalRerankRun).where(
                MultimodalRerankRun.caption_slate_id == completed_slate.id
            )
        )
        parent_run = session.scalar(
            select(IntelligenceAgentRun).where(
                IntelligenceAgentRun.run_key == f"caption-slate-{completed_slate.id}-adaptive-v1"
            )
        )
        assert parent_run is not None
        parent_steps = session.scalars(
            select(IntelligenceAgentStep)
            .where(IntelligenceAgentStep.agent_run_id == parent_run.id)
            .order_by(IntelligenceAgentStep.sequence)
        ).all()
    assert len(completed_records) == sum(len(attempt["candidates"]) for attempt in raw_attempts)
    prepared = [row for row in completed_records if row.display_order is not None]
    assert len(prepared) == 3
    assert all(row.eligible for row in prepared)
    assert all(not row.displayed for row in prepared)
    assert sorted(row.display_order for row in prepared) == [
        1,
        2,
        3,
    ]
    generated_angles = {candidate["editorial_angle"] for candidate in raw_attempts[0]["candidates"]}
    assert set(editorial_brief["recommended_angles"]) <= generated_angles
    assert set(cited["historical_post_ids"]) <= set(supplied["historical_post_ids"])
    assert set(cited["feedback_signal_ids"]) <= set(supplied["feedback_signal_ids"])
    assert reranker is not None
    assert reranker.status == "shadow_completed"
    assert reranker.label_source == "policy"
    assert parent_run.status == "completed"
    assert parent_run.capability == "editorial_pipeline"
    parent_capabilities = [step.capability for step in parent_steps]
    assert parent_capabilities[:6] == [
        "retrieve_evidence",
        "evidence_coverage",
        "content_mode_selection",
        "editorial_angle_planning",
        "caption_generation",
        "caption_verification",
    ]
    assert parent_capabilities[-2:] == [
        "joint_multimodal_reranking",
        "slate_diversity_optimization",
    ]
    assert parent_capabilities[6:-2] in ([], ["claim_level_grounding"])
    assert parent_steps[-1].artifact_type == "caption_slate"
    assert parent_steps[-1].artifact_id == str(completed_slate.id)

    mined = HardNegativeMiningService(database, settings).mine(limit=50)
    assert int(mined["created"]) > 0
    assert mined["label_source"] == "policy"
    assert mined["human_labels_created"] == 0
    assert mined["product_training_eligible"] is False
    with database.session() as session:
        hard_negatives = session.scalars(
            select(PairwisePreference).where(
                PairwisePreference.preference_source == "hard_negative_mining"
            )
        ).all()
    assert hard_negatives
    assert {row.label_source for row in hard_negatives} == {"policy"}

    abstained = await CaptionService(
        database,
        settings,
        runtime=UngroundedCaptionRuntime(),
    ).generate(candidate_id)
    assert abstained.abstained is True
    assert abstained.recommended == ""
    assert abstained.alternatives == []
    with database.session() as session:
        abstained_slate = session.get(CaptionSlate, abstained.slate_id)
        assert abstained_slate is not None
        failed_records = session.scalars(
            select(CaptionCandidateRecord)
            .where(CaptionCandidateRecord.caption_slate_id == abstained_slate.id)
            .order_by(CaptionCandidateRecord.rank)
        ).all()
        model_run = session.get(ModelRun, abstained_slate.model_run_id)
        assert model_run is not None
        structured_output = json.loads(model_run.structured_output_json)
        abstained_parent = session.scalar(
            select(IntelligenceAgentRun).where(
                IntelligenceAgentRun.run_key == f"caption-slate-{abstained_slate.id}-adaptive-v1"
            )
        )
    assert abstained_slate.status == "abstained"
    assert len(failed_records) == 6
    assert all(not row.eligible for row in failed_records)
    assert all(json.loads(row.verifier_result_json)["passed"] is False for row in failed_records)
    assert all(not row.displayed for row in failed_records)
    assert len(structured_output["attempts"]) == 2
    assert abstained_parent is not None
    assert abstained_parent.status == "abstained"
    assert abstained_parent.error_summary

    guarded = await CaptionService(
        database,
        settings,
        runtime=FalsePremiseCaptionRuntime(),
    ).generate(candidate_id)
    assert guarded.abstained is False
    with database.session() as session:
        guarded_records = session.scalars(
            select(CaptionCandidateRecord)
            .where(CaptionCandidateRecord.caption_slate_id == guarded.slate_id)
            .order_by(CaptionCandidateRecord.rank)
        ).all()
    false_premise_rows = [
        row for row in guarded_records if "hidden under the blanket" in row.text.casefold()
    ]
    assert len(false_premise_rows) == 1
    false_premise = false_premise_rows[0]
    assert false_premise.eligible is False
    assert false_premise.displayed is False
    assert false_premise.grounding_score == pytest.approx(0.08)
    assert "independent_grounding_unsupported" in json.loads(false_premise.exclusion_reasons_json)
    assert "critical_claim_failure" in json.loads(false_premise.exclusion_reasons_json)
    claim_audit = json.loads(false_premise.claim_verification_json)
    assert claim_audit["passed"] is False
    assert claim_audit["semantic_layer_status"] == (
        "independent_frontier_visual_verifier_completed"
    )
    assert claim_audit["critical_failures"] == ["What snack is hidden under the blanket?"]
    assert "no hidden snack is visible" in claim_audit["claims"][0]["evidence"]
