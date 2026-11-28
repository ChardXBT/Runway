from __future__ import annotations

import hashlib
import multiprocessing as mp
import time
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CandidateImage,
    GenerationRun,
    MediaAsset,
    Proposal,
    SearchRun,
)
from runway.db.repositories import get_channel
from runway.proposals.service import ProposalService


def _seed_review_proposal(
    database: Database,
    settings: Settings,
    *,
    suffix: str,
    planned_offset_days: int,
) -> int:
    image_bytes = f"fixture image {suffix}".encode()
    relative = Path("media") / "candidates" / f"{suffix}.img"
    image_path = settings.resolved_data_dir / relative
    image_path.parent.mkdir(parents=True, exist_ok=True)
    image_path.write_bytes(image_bytes)
    planned_date = datetime.now(ZoneInfo(settings.timezone)).date() + timedelta(
        days=planned_offset_days
    )
    planned_at = datetime(
        planned_date.year,
        planned_date.month,
        planned_date.day,
        9,
        0,
        tzinfo=ZoneInfo(settings.timezone),
    )
    with database.session() as session:
        channel = get_channel(session, settings.channel_handle)
        search_run = SearchRun(
            channel_id=channel.id,
            style_profile_id=None,
            query_plan_json="{}",
            provider="fixture",
            status="completed",
            result_count=1,
        )
        media = MediaAsset(
            kind="candidate",
            local_path=relative.as_posix(),
            original_url=f"https://fixture.invalid/{suffix}.img",
            source_page_url="https://fixture.invalid/page",
            source_domain="fixture.invalid",
            original_filename=f"{suffix}.img",
            mime_type="image/png",
            width=640,
            height=480,
            file_size=len(image_bytes),
            sha256=hashlib.sha256(image_bytes).hexdigest(),
            perceptual_hash=f"{suffix:0>64}"[-64:],
            quality_metrics_json="{}",
            rights_status="creator_owned",
        )
        session.add_all([search_run, media])
        session.flush()
        candidate = CandidateImage(
            search_run_id=search_run.id,
            media_asset_id=media.id,
            preview_asset_id=None,
            search_query="fixture",
            result_rank=1,
            source_page_url="https://fixture.invalid/page",
            direct_image_url=f"https://fixture.invalid/{suffix}.img",
            source_domain="fixture.invalid",
            rights_status="creator_owned",
            original_width=640,
            original_height=480,
            quality_score=0.9,
            style_score=0.9,
            novelty_score=0.9,
            caption_potential_score=0.9,
            source_risk_score=0.0,
            final_rank_score=0.9,
        )
        generation_run = GenerationRun(
            channel_id=channel.id,
            style_profile_id=None,
            start_date=planned_date,
            days=1,
            status="completed",
        )
        session.add_all([candidate, generation_run])
        session.flush()
        proposal = Proposal(
            generation_run_id=generation_run.id,
            channel_id=channel.id,
            candidate_image_id=candidate.id,
            caption_slate_id=None,
            planned_publish_at=planned_at.isoformat(),
            scheduled_publish_at=None,
            recommended_caption=f"Fixture caption {suffix}?",
            alternative_captions_json="[]",
            caption_rationale="fixture",
            caption_confidence=0.9,
            caption_reference_post_ids_json="[]",
            factual_uncertainty_warning=None,
            final_caption=f"Fixture caption {suffix}?",
            status="needs_review",
            selection_reason="fixture",
            style_score=0.9,
            novelty_score=0.9,
            quality_score=0.9,
        )
        session.add(proposal)
        session.flush()
        return int(proposal.id)


def _decide_in_child(
    data_dir: str,
    proposal_id: int,
    action: str,
    start_barrier: object,
    result_queue: object,
) -> None:
    database = None
    try:
        settings = Settings(data_dir=Path(data_dir), discovery_secondary_topics="")
        database = Database(settings)
        import runway.proposals.service as proposal_service_module

        original_copy2 = proposal_service_module.shutil.copy2

        def slow_copy2(source: object, destination: object) -> object:
            # Approval otherwise commits within milliseconds. This small delay keeps
            # both workers inside their overlapping scheduling critical sections.
            time.sleep(0.35)
            return original_copy2(source, destination)  # type: ignore[arg-type]

        proposal_service_module.shutil.copy2 = slow_copy2  # type: ignore[assignment]
        service = ProposalService(database, settings)
        start_barrier.wait()  # type: ignore[attr-defined]
        if action == "approve":
            result = service.approve(proposal_id)
        elif action == "accept":
            result = service.accept_to_lineup(proposal_id, f"Accepted caption {proposal_id}?")
        else:
            raise ValueError(f"unknown action {action}")
        result_queue.put(  # type: ignore[attr-defined]
            {
                "outcome": "ok",
                "proposal_id": proposal_id,
                "scheduled_publish_at": result["scheduled_publish_at"],
            }
        )
    except BaseException as exc:  # pragma: no cover - reported in the parent assertion
        result_queue.put(  # type: ignore[attr-defined]
            {
                "outcome": "error",
                "proposal_id": proposal_id,
                "error": f"{type(exc).__name__}: {exc}",
                "traceback": traceback.format_exc(),
            }
        )
    finally:
        if database is not None:
            database.engine.dispose()


def test_two_process_approval_and_acceptance_cannot_claim_the_same_lineup_date(
    database: Database,
    settings: Settings,
) -> None:
    first_id = _seed_review_proposal(
        database,
        settings,
        suffix="first",
        planned_offset_days=20,
    )
    second_id = _seed_review_proposal(
        database,
        settings,
        suffix="second",
        planned_offset_days=21,
    )
    database.engine.dispose()

    context = mp.get_context("spawn")
    start_barrier = context.Barrier(3)
    result_queue = context.Queue()
    workers = [
        context.Process(
            target=_decide_in_child,
            args=(
                str(settings.resolved_data_dir),
                first_id,
                "approve",
                start_barrier,
                result_queue,
            ),
        ),
        context.Process(
            target=_decide_in_child,
            args=(
                str(settings.resolved_data_dir),
                second_id,
                "accept",
                start_barrier,
                result_queue,
            ),
        ),
    ]
    for worker in workers:
        worker.start()
    start_barrier.wait(timeout=10)
    for worker in workers:
        worker.join(timeout=30)
    for worker in workers:
        if worker.is_alive():
            worker.terminate()
            worker.join()
    results = [result_queue.get(timeout=2) for _ in workers]

    errors = [result for result in results if result["outcome"] != "ok"]
    assert not errors, errors
    assert all(worker.exitcode == 0 for worker in workers)

    verifier_database = Database(settings)
    try:
        proposals = ProposalService(verifier_database, settings)
        timezone = ZoneInfo(settings.timezone)
        dates = {
            datetime.fromisoformat(str(proposals.detail(proposal_id)["scheduled_publish_at"]))
            .astimezone(timezone)
            .date()
            for proposal_id in (first_id, second_id)
        }
    finally:
        verifier_database.engine.dispose()
    assert len(dates) == 2
