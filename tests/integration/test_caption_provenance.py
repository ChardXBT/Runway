from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from runway.analysis.runtime import MockAgentRuntime
from runway.analysis.schemas import CaptionCandidate, CaptionCandidateSet
from runway.analysis.service import AnalysisService
from runway.captions.service import CaptionService
from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CaptionCandidateRecord,
    CaptionSlate,
    ModelRun,
)
from runway.discovery.service import DiscoveryService
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
    assert len(completed_records) == sum(len(attempt["candidates"]) for attempt in raw_attempts)
    displayed = [row for row in completed_records if row.displayed]
    assert len(displayed) == 3
    assert all(row.eligible for row in displayed)
    assert [row.display_order for row in displayed] == [1, 2, 3]

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
    assert abstained_slate.status == "abstained"
    assert len(failed_records) == 6
    assert all(not row.eligible for row in failed_records)
    assert all(json.loads(row.verifier_result_json)["passed"] is False for row in failed_records)
    assert all(not row.displayed for row in failed_records)
    assert len(structured_output["attempts"]) == 2
