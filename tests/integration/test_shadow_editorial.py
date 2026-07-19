from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from runway.analysis.runtime import MockAgentRuntime
from runway.analysis.service import AnalysisService
from runway.capture.service import CaptureService
from runway.db.models import FeedbackSignal, Proposal, ShadowEditorialDecision
from runway.discovery.service import DiscoveryService
from runway.intelligence.profile import StyleProfileService
from runway.intelligence.shadow_editorial import ShadowEditorialService
from runway.proposals.service import ProposalService


async def test_shadow_reviewer_never_mutates_proposals_or_creator_feedback(
    database,
    settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    await AnalysisService(database, settings).analyze_history()
    await StyleProfileService(database, settings).build()
    await DiscoveryService(database, settings).discover(days=2, provider_name="fixture")
    generation = await ProposalService(database, settings).generate_batch(
        days=2,
        start_date=date(2030, 5, 1),
    )
    proposal_ids = [int(value) for value in generation["proposal_ids"]]
    with database.session() as session:
        statuses_before = dict(
            session.execute(
                select(Proposal.id, Proposal.status).where(Proposal.id.in_(proposal_ids))
            ).all()
        )
        feedback_before = int(session.scalar(select(func.count(FeedbackSignal.id))) or 0)

    report = await ShadowEditorialService(
        database,
        settings,
        runtime=MockAgentRuntime(),
    ).review_pending(limit=10)

    with database.session() as session:
        statuses_after = dict(
            session.execute(
                select(Proposal.id, Proposal.status).where(Proposal.id.in_(proposal_ids))
            ).all()
        )
        feedback_after = int(session.scalar(select(func.count(FeedbackSignal.id))) or 0)
        decisions = session.scalars(select(ShadowEditorialDecision)).all()
    assert statuses_after == statuses_before
    assert feedback_after == feedback_before
    assert len(decisions) == 2
    assert all(row.label_source == "synthetic" for row in decisions)
    assert all(row.training_eligible is False for row in decisions)
    assert report["training_eligible_count"] == 0
    assert report["safety"]["can_schedule_or_publish"] is False
