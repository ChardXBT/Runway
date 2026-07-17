from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import select

from leeway.analysis.service import AnalysisService
from leeway.captions.feedback import CaptionFeedbackService
from leeway.captions.service import CaptionService
from leeway.capture.service import CaptureService
from leeway.config import Settings
from leeway.db.base import Database
from leeway.db.models import CandidateImage, CaptionFeedback
from leeway.discovery.service import DiscoveryService
from leeway.intelligence.profile import StyleProfileService
from leeway.proposals.service import ProposalService


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
    assert captions.alternatives[0].endswith(".")
    assert captions.alternatives[1].endswith(".")

    proposals = ProposalService(database, settings)
    generated = await proposals.generate_batch(days=1, start_date=date(2030, 3, 5))
    proposal_id = int(generated["proposal_ids"][0])
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
    assert len(rows) == 3
