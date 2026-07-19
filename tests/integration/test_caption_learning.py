from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select

from runway.analysis.service import AnalysisService
from runway.captions.feedback import CaptionFeedbackService
from runway.captions.service import CaptionService
from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CandidateImage,
    CaptionCandidateRecord,
    CaptionExposure,
    CaptionFeedback,
    FeedbackSignal,
    PairwisePreference,
)
from runway.discovery.service import DiscoveryService
from runway.intelligence.profile import StyleProfileService
from runway.proposals.service import ProposalService


@pytest.mark.asyncio
async def test_grounded_question_first_caption_and_feedback_memory(
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

    with database.session() as session:
        candidate = session.get(CandidateImage, candidate_id)
        assert candidate is not None
        candidate.detected_topic_json = json.dumps(
            {
                "franchise": "The Simpsons",
                "characters": ["Homer Simpson"],
                "scene_archetype": "reaction",
                "composition": "centered close-up",
                "emotion": "excitement",
                "confidence": 0.99,
            }
        )

    captions = await CaptionService(database, settings).generate(candidate_id)
    assert captions.recommended == "Why is Homer so excited?"
    assert all(caption.endswith((".", "?", "!")) for caption in captions.alternatives)

    proposals = ProposalService(database, settings)
    generated = await proposals.generate_batch(days=1, start_date=date(2030, 3, 5))
    proposal_id = int(generated["proposal_ids"][0])
    selected = proposals.select_alternative(proposal_id, 0)
    assert selected["status"] == "needs_review"
    with database.session() as session:
        selected_pair_count = len(
            session.scalars(
                select(PairwisePreference).where(PairwisePreference.proposal_id == proposal_id)
            ).all()
        )
    approved = proposals.approve(proposal_id)
    assert approved["status"] == "approved"
    with database.session() as session:
        accepted_pair_count = len(
            session.scalars(
                select(PairwisePreference).where(PairwisePreference.proposal_id == proposal_id)
            ).all()
        )
    assert accepted_pair_count == selected_pair_count
    edited = proposals.edit_caption(
        proposal_id,
        "What has Homer so excited?",
        reason_codes=["prefer_open_question"],
        image_verdict="good",
        note="Specific open questions invite better replies.",
    )
    assert edited["final_caption"] == "What has Homer so excited?"
    feedback = edited["caption_feedback"]
    assert isinstance(feedback, list)
    assert feedback[-1]["verdict"] == "edited"
    assert feedback[-1]["preferred_structure"] == "open_question"

    service = CaptionFeedbackService(database)
    first = service.record(
        proposal_id,
        verdict="preferred",
        preferred_caption="Why is Homer so excited?",
        preferred_structure="open_question",
        reason_codes=["prefer_open_question"],
        image_verdict="good",
        note="Strong community prompt.",
    )
    second = service.record(
        proposal_id,
        verdict="preferred",
        preferred_caption="Why is Homer so excited?",
        preferred_structure="open_question",
        reason_codes=["prefer_open_question"],
        image_verdict="good",
        note="Strong community prompt.",
    )
    assert first["id"] == second["id"]

    service.record(
        proposal_id,
        verdict="rejected",
        generated_caption="Homer is very excited.",
        reason_codes=["too_generic", "prefer_open_question"],
        image_verdict="good",
        note="Flat description instead of an invitation to respond.",
    )
    context = service.context_for_candidate(candidate_id)
    assert any(
        item["preferred_caption"] == "Why is Homer so excited?"
        for item in context["positive_examples"]
    )
    assert any(item["caption"] == "Homer is very excited." for item in context["negative_examples"])
    assert context["editorial_policy"]["recommended_structure"] == "open_question"

    with database.session() as session:
        rows = session.scalars(
            select(CaptionFeedback).where(CaptionFeedback.proposal_id == proposal_id)
        ).all()
        exposure = session.scalar(
            select(CaptionExposure).where(CaptionExposure.proposal_id == proposal_id)
        )
        pairwise = session.scalars(
            select(PairwisePreference).where(PairwisePreference.proposal_id == proposal_id)
        ).all()
        feedback_targets = set(
            session.scalars(
                select(FeedbackSignal.target).where(FeedbackSignal.proposal_id == proposal_id)
            )
        )
        human_edits = session.scalars(
            select(CaptionCandidateRecord).where(
                CaptionCandidateRecord.caption_slate_id == exposure.caption_slate_id,
                CaptionCandidateRecord.origin == "human_edit",
            )
        ).all()
    assert len(rows) == 5
    assert {row.verdict for row in rows} == {
        "selected",
        "accepted",
        "edited",
        "preferred",
        "rejected",
    }
    assert exposure is not None
    assert exposure.decision_type == "edited"
    assert {"selected", "human_edit"} <= {row.preference_source for row in pairwise}
    assert len(human_edits) == 1
    assert human_edits[0].text == "What has Homer so excited?"
    assert human_edits[0].parent_candidate_id is not None
    assert human_edits[0].source_proposal_event_id is not None
    assert human_edits[0].feature_snapshot_hash
    assert human_edits[0].representation_record_id is not None
    verifier_result = json.loads(human_edits[0].verifier_result_json)
    assert verifier_result["passed"] is True
    assert human_edits[0].verifier_version == "caption-verifier-v1"
    assert all(row.idempotency_key for row in pairwise)
    assert all(row.source_proposal_event_id for row in pairwise)
    assert all(row.feature_snapshot_hash for row in pairwise)
    assert feedback_targets == {"caption", "image", "pairing"}
    first_reconciliation = service.reconcile_legacy()
    second_reconciliation = service.reconcile_legacy()
    assert first_reconciliation["created"] == 0
    assert second_reconciliation["created"] == 0
    assert first_reconciliation["canonical_signals"] > 0
    assert second_reconciliation["canonical_signals"] == first_reconciliation["canonical_signals"]
