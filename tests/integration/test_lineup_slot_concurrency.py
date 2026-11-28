from __future__ import annotations

import hashlib
import multiprocessing as mp
import time
import traceback
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from alembic.config import Config
from sqlalchemy import select

from alembic import command
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CandidateImage,
    GenerationRun,
    LineupSlotClaim,
    MediaAsset,
    Proposal,
    SearchRun,
)
from runway.db.repositories import get_channel
from runway.proposals.schedule_slots import (
    channel_slot_date,
    claim_next_available_slot,
    synchronize_lineup_slot_claims,
)
from runway.proposals.service import ProposalService
from runway.publishing.youtube import YouTubeBrowserPublisher


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


def test_same_instant_is_one_channel_slot_even_with_different_stored_offsets() -> None:
    toronto = ZoneInfo("America/Toronto")
    local = datetime(2026, 11, 20, 10, 0, tzinfo=toronto)
    utc_equivalent = local.astimezone(UTC)

    assert local.isoformat() != utc_equivalent.isoformat()
    assert channel_slot_date(local.isoformat(), toronto) == channel_slot_date(
        utc_equivalent.isoformat(),
        toronto,
    )


def test_two_active_rows_on_the_same_timestamp_fail_closed(
    database: Database,
    settings: Settings,
) -> None:
    first_id = _seed_review_proposal(
        database,
        settings,
        suffix="conflict-first",
        planned_offset_days=30,
    )
    second_id = _seed_review_proposal(
        database,
        settings,
        suffix="conflict-second",
        planned_offset_days=31,
    )
    toronto = ZoneInfo(settings.timezone)
    local = datetime.now(toronto) + timedelta(days=4)
    local = local.replace(hour=10, minute=0, second=0, microsecond=0)
    utc_equivalent = local.astimezone(UTC).isoformat()

    with (
        pytest.raises(ValueError, match="multiple active RunWay posts"),
        database.session() as session,
    ):
        first = session.get(Proposal, first_id)
        second = session.get(Proposal, second_id)
        assert first is not None
        assert second is not None
        first.status = "approved"
        first.scheduled_publish_at = local.isoformat()
        second.status = "approved"
        second.scheduled_publish_at = utc_equivalent
        session.flush()
        synchronize_lineup_slot_claims(session, first.channel_id, settings.timezone)


def test_dst_gap_default_time_claims_the_real_instant_on_that_date(
    database: Database,
    settings: Settings,
) -> None:
    proposal_id = _seed_review_proposal(
        database,
        settings,
        suffix="dst-gap",
        planned_offset_days=32,
    )
    with database.session() as session:
        channel = get_channel(session, settings.channel_handle)
        channel.default_post_time = "02:30"

    toronto = ZoneInfo(settings.timezone)
    with database.immediate_session() as session:
        proposal = session.get(Proposal, proposal_id)
        assert proposal is not None
        claimed = claim_next_available_slot(
            session,
            proposal,
            timezone_name=settings.timezone,
            default_time="02:30",
            now=datetime(2026, 3, 7, 4, 0, tzinfo=toronto),
        )

    local = claimed.astimezone(toronto)
    assert local.date().isoformat() == "2026-03-08"
    assert (local.hour, local.minute) == (3, 30)
    with database.session() as session:
        claim = session.scalar(
            select(LineupSlotClaim).where(LineupSlotClaim.proposal_id == proposal_id)
        )
        assert claim is not None
        assert claim.slot_date.isoformat() == "2026-03-08"


def test_stale_cancelled_and_rejected_schedule_rows_do_not_block_a_slot(
    database: Database,
    settings: Settings,
) -> None:
    proposals = ProposalService(database, settings)
    expected = proposals.next_available_slot()
    stale_ids = [
        _seed_review_proposal(
            database,
            settings,
            suffix=f"stale-{index}",
            planned_offset_days=40 + index,
        )
        for index in range(3)
    ]
    with database.session() as session:
        channel = get_channel(session, settings.channel_handle)
        for proposal_id, status in zip(
            stale_ids,
            ["cancelled", "rejected", "needs_review"],
            strict=True,
        ):
            proposal = session.get(Proposal, proposal_id)
            assert proposal is not None
            proposal.status = status
            proposal.scheduled_publish_at = expected.isoformat()
        stale_claim = LineupSlotClaim(
            channel_id=channel.id,
            proposal_id=stale_ids[0],
            slot_date=expected.astimezone(ZoneInfo(settings.timezone)).date(),
            scheduled_publish_at=expected.isoformat(),
        )
        session.add(stale_claim)

    fresh_id = _seed_review_proposal(
        database,
        settings,
        suffix="fresh-after-stale",
        planned_offset_days=50,
    )
    approved = proposals.approve(fresh_id)

    assert approved["scheduled_publish_at"] == expected.isoformat()
    with database.session() as session:
        claims = session.scalars(select(LineupSlotClaim)).all()
        assert len(claims) == 1
        assert claims[0].proposal_id == fresh_id


def test_reapproval_retry_recreates_one_claim_and_publish_failure_still_reserves_it(
    database: Database,
    settings: Settings,
) -> None:
    proposals = ProposalService(database, settings)
    proposal_id = _seed_review_proposal(
        database,
        settings,
        suffix="retry",
        planned_offset_days=51,
    )
    first = proposals.approve(proposal_id)
    edited = proposals.edit_caption(proposal_id, "Retry the caption?")
    assert edited["status"] == "needs_review"
    with database.session() as session:
        assert session.scalars(select(LineupSlotClaim)).all() == []

    approved_again = proposals.approve(proposal_id)
    assert approved_again["scheduled_publish_at"] == first["scheduled_publish_at"]
    with database.session() as session:
        proposal = session.get(Proposal, proposal_id)
        assert proposal is not None
        proposal.status = "publish_failed"
    other_id = _seed_review_proposal(
        database,
        settings,
        suffix="retry-other",
        planned_offset_days=52,
    )
    other = proposals.approve(other_id)
    toronto = ZoneInfo(settings.timezone)
    first_date = (
        datetime.fromisoformat(str(approved_again["scheduled_publish_at"]))
        .astimezone(toronto)
        .date()
    )
    other_date = (
        datetime.fromisoformat(str(other["scheduled_publish_at"])).astimezone(toronto).date()
    )
    assert other_date != first_date
    with database.session() as session:
        claims = session.scalars(select(LineupSlotClaim)).all()
        assert len(claims) == 2
        assert len({claim.proposal_id for claim in claims}) == 2


@pytest.mark.asyncio
async def test_lineup_swaps_and_removals_keep_claim_rows_in_sync(
    database: Database,
    settings: Settings,
) -> None:
    proposals = ProposalService(database, settings)
    first_id = _seed_review_proposal(
        database,
        settings,
        suffix="swap-first",
        planned_offset_days=60,
    )
    second_id = _seed_review_proposal(
        database,
        settings,
        suffix="swap-second",
        planned_offset_days=61,
    )
    first_before = proposals.approve(first_id)
    second_before = proposals.approve(second_id)
    with database.session() as session:
        for proposal_id in (first_id, second_id):
            proposal = session.get(Proposal, proposal_id)
            assert proposal is not None
            proposal.status = "internally_scheduled"

    toronto = ZoneInfo(settings.timezone)
    second_date = (
        datetime.fromisoformat(str(second_before["scheduled_publish_at"]))
        .astimezone(toronto)
        .date()
    )
    publisher = YouTubeBrowserPublisher(database, settings)
    swapped = await publisher.update_lineup(
        first_id,
        final_caption=None,
        new_date=second_date,
    )
    assert swapped["swapped_with"] == second_id

    first_after = proposals.detail(first_id)
    second_after = proposals.detail(second_id)
    assert first_after["scheduled_publish_at"] == second_before["scheduled_publish_at"]
    assert second_after["scheduled_publish_at"] == first_before["scheduled_publish_at"]
    with database.session() as session:
        claims = session.scalars(select(LineupSlotClaim)).all()
        assert len(claims) == 2
        by_proposal = {claim.proposal_id: claim for claim in claims}
        assert by_proposal[first_id].scheduled_publish_at == first_after["scheduled_publish_at"]
        assert by_proposal[second_id].scheduled_publish_at == second_after["scheduled_publish_at"]

    await publisher.remove_from_lineup(first_id)
    with database.session() as session:
        claims = session.scalars(select(LineupSlotClaim)).all()
        assert [claim.proposal_id for claim in claims] == [second_id]

    replacement_id = _seed_review_proposal(
        database,
        settings,
        suffix="after-removal",
        planned_offset_days=62,
    )
    replacement = proposals.approve(replacement_id)
    assert replacement["scheduled_publish_at"] == first_after["scheduled_publish_at"]


def test_0013_migration_backfills_active_claims_using_channel_timezone_dates(
    database: Database,
    settings: Settings,
) -> None:
    proposals = ProposalService(database, settings)
    proposal_id = _seed_review_proposal(
        database,
        settings,
        suffix="migration-backfill",
        planned_offset_days=70,
    )
    approved = proposals.approve(proposal_id)
    scheduled = str(approved["scheduled_publish_at"])
    database.engine.dispose()

    config = Config(str(settings.project_root / "alembic.ini"))
    config.set_main_option("script_location", str(settings.project_root / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.downgrade(config, "0012_creator_platforms")
    command.upgrade(config, "head")

    migrated = Database(settings)
    try:
        with migrated.session() as session:
            claim = session.scalar(
                select(LineupSlotClaim).where(LineupSlotClaim.proposal_id == proposal_id)
            )
            assert claim is not None
            assert claim.scheduled_publish_at == scheduled
            expected_date = (
                datetime.fromisoformat(scheduled).astimezone(ZoneInfo(settings.timezone)).date()
            )
            assert claim.slot_date == expected_date
    finally:
        migrated.engine.dispose()


def test_0013_migration_refuses_preexisting_active_duplicate_channel_dates(
    database: Database,
    settings: Settings,
) -> None:
    first_id = _seed_review_proposal(
        database,
        settings,
        suffix="migration-conflict-first",
        planned_offset_days=71,
    )
    second_id = _seed_review_proposal(
        database,
        settings,
        suffix="migration-conflict-second",
        planned_offset_days=72,
    )
    toronto = ZoneInfo(settings.timezone)
    local = datetime.now(toronto) + timedelta(days=5)
    local = local.replace(hour=10, minute=0, second=0, microsecond=0)
    with database.session() as session:
        first = session.get(Proposal, first_id)
        second = session.get(Proposal, second_id)
        assert first is not None
        assert second is not None
        first.status = "approved"
        first.scheduled_publish_at = local.isoformat()
        second.status = "approved"
        second.scheduled_publish_at = local.astimezone(UTC).isoformat()
    database.engine.dispose()

    config = Config(str(settings.project_root / "alembic.ini"))
    config.set_main_option("script_location", str(settings.project_root / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.downgrade(config, "0012_creator_platforms")
    with pytest.raises(RuntimeError, match="already share channel date"):
        command.upgrade(config, "head")
