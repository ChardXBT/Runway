from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from runway.analysis.service import AnalysisService
from runway.captions.feedback import CaptionFeedbackService
from runway.captions.service import CaptionService
from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db import initialize_database
from runway.db.models import (
    CandidateImage,
    CaptionCandidateRecord,
    IntelligenceAgentRun,
    IntelligenceRetrievalRun,
    RepresentationSet,
)
from runway.discovery.service import DiscoveryService
from runway.intelligence.doctor import IntelligenceDoctor
from runway.intelligence.profile import StyleProfileService
from runway.intelligence.representation_sets import RepresentationSetService
from runway.intelligence.retrieval import RetrievalService


def _complete_set(
    lifecycle: RepresentationSetService,
    representation_set_id: int,
) -> None:
    for _ in range(20):
        result = lifecycle.backfill(
            representation_set_id,
            batch_size=20,
        )
        if result["complete"] == result["expected"]:
            return
    raise AssertionError(
        f"representation set {representation_set_id} did not complete"
    )


@pytest.mark.asyncio
async def test_deterministic_intelligence_flywheel_end_to_end(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "intelligence-flywheel",
        agent_runtime="mock",
        publishing_enabled=False,
    )
    database = initialize_database(settings)

    captured = CaptureService(database, settings).run_fixture()
    assert captured.status == "completed"
    await AnalysisService(database, settings).analyze_history()
    await StyleProfileService(database, settings).build()

    lifecycle = RepresentationSetService(database, settings)
    active_set_ids: list[int] = []
    for modality in ("text", "image", "multimodal"):
        planned = lifecycle.plan_history(modality)  # type: ignore[arg-type]
        set_id = int(planned["representation_set_id"])
        _complete_set(lifecycle, set_id)
        validation = lifecycle.validate(set_id)
        assert validation["valid"] is True
        activated = lifecycle.activate(
            set_id,
            reason=f"deterministic e2e {modality} baseline",
        )
        assert activated["active"] is True
        active_set_ids.append(set_id)

    discovery = DiscoveryService(database, settings)
    discovery_run = await discovery.discover(
        provider_name="fixture",
        dry_run=True,
    )
    candidates = discovery.list_candidates(
        run_id=int(discovery_run["run_id"]),
        accepted_only=True,
    )
    assert candidates
    candidate_id = int(candidates[0]["id"])
    with database.session() as session:
        candidate = session.get(CandidateImage, candidate_id)
        assert candidate is not None
        media_id = candidate.media_asset_id

    retrieval = RetrievalService(database, settings)
    first_context = retrieval.context_for_candidate(media_id)
    second_context = retrieval.context_for_candidate(media_id)
    assert first_context["retrieval_run_id"] != second_context["retrieval_run_id"]
    inspected = retrieval.inspect_run(int(second_context["retrieval_run_id"]))
    assert inspected["cache_diagnostics"]["recomputations"] == 0
    assert inspected["cache_diagnostics"]["active_hits"] > 0
    assert {
        value["id"] for value in inspected["representation_sets"].values()
    } == set(active_set_ids)

    caption_options = await CaptionService(database, settings).generate(
        candidate_id
    )
    assert caption_options.recommended.endswith(("?", "!", "."))
    assert caption_options.slate_id is not None
    reconciliation = CaptionFeedbackService(
        database,
        settings,
    ).reconcile_legacy()
    assert reconciliation["legacy_rows"] == 0
    assert reconciliation["canonical_signals"] == 0

    report = IntelligenceDoctor(database, settings).run()
    assert report.critical_count == 0, report.human_text()
    assert not any(
        finding.code.endswith(".internal_error")
        for finding in report.findings
    )
    with database.session() as session:
        assert (
            session.scalar(
                select(func.count(RepresentationSet.id)).where(
                    RepresentationSet.active.is_(True)
                )
            )
            == 3
        )
        assert (
            session.scalar(select(func.count(IntelligenceRetrievalRun.id)))
            or 0
        ) >= 3
        assert (
            session.scalar(select(func.count(CaptionCandidateRecord.id))) or 0
        ) >= 3
        assert (
            session.scalar(select(func.count(IntelligenceAgentRun.id))) or 0
        ) >= 1
