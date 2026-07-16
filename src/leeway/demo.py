from __future__ import annotations

from datetime import date

from leeway.analysis.service import AnalysisService
from leeway.capture.service import CaptureService
from leeway.catalog.service import CatalogService
from leeway.config import Settings
from leeway.db import Database, initialize_database
from leeway.discovery.service import DiscoveryService
from leeway.intelligence.profile import StyleProfileService
from leeway.proposals.service import ProposalService
from leeway.publishing.internal import InternalPublisher


def _required_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise TypeError(f"{field} must be an integer")
    return int(value)


async def run_fixture_demo(
    settings: Settings,
    *,
    start_date: date = date(2026, 3, 5),
) -> dict[str, object]:
    database = initialize_database(settings)
    capture = CaptureService(database, settings)
    paused = capture.run_fixture(max_posts=4, resume=True)
    resumed = capture.run_fixture(resume=True)
    idempotent = capture.run_fixture(resume=True)
    verification = CatalogService(database, settings).verify()

    analysis = await AnalysisService(database, settings).analyze_history(resume=True)
    profiles = StyleProfileService(database, settings)
    profile = await profiles.build()
    evaluation = profiles.evaluate()
    discovery = await DiscoveryService(database, settings).discover(
        days=10, provider_name="fixture", dry_run=True
    )

    proposals = ProposalService(database, settings)
    generation = await proposals.generate_batch(days=10, start_date=start_date)
    rows = proposals.list_proposals()
    first_id, second_id, third_id = (_required_int(row["id"], "proposal.id") for row in rows[:3])
    discovery_accepted = _required_int(discovery["accepted"], "discovery.accepted")
    generated_ids = generation["proposal_ids"]
    if not isinstance(generated_ids, list):
        raise TypeError("generation.proposal_ids must be a list")

    proposals.edit_caption(first_id, "Human-edited fixture proof caption.")
    await proposals.regenerate_captions(first_id)
    proposals.reject(second_id, "fixture proof rejection")
    await proposals.replace_image(second_id)
    proposals.approve(third_id)
    scheduled = await InternalPublisher(database, settings).schedule_post(third_id)
    queue = proposals.queue_status(days=10, start_date=start_date)
    database.engine.dispose()

    restarted = Database(settings)
    persisted_first = ProposalService(restarted, settings).detail(first_id)
    persisted_third = ProposalService(restarted, settings).detail(third_id)
    restarted.engine.dispose()

    assertions = {
        "capture_paused": paused.status == "paused",
        "capture_resumed_same_run": resumed.run_id == paused.run_id,
        "capture_idempotent": idempotent.posts_created == 0,
        "catalogue_verified": verification["total_posts"] == 12,
        "analysis_complete": analysis["failed"] == 0,
        "profile_built": profile["version"] == 1,
        "evaluation_written": evaluation["holdout_samples"] > 0,
        "discovery_has_ten": discovery_accepted >= 10,
        "ten_proposals": len(generated_ids) == 10,
        "queue_covered": queue["coverage"] == 10 and not queue["conflicts"],
        "caption_persisted": (
            persisted_first["final_caption"] == "Human-edited fixture proof caption."
        ),
        "internal_schedule_persisted": persisted_third["status"] == "internally_scheduled",
        "no_external_publish": scheduled.external_id is None,
    }
    if not all(assertions.values()):
        failed = [name for name, passed in assertions.items() if not passed]
        raise AssertionError(f"fixture proof failed: {', '.join(failed)}")
    return {
        "status": "passed",
        "data_dir": str(settings.resolved_data_dir),
        "assertions": assertions,
        "capture": {
            "run_id": resumed.run_id,
            "posts": verification["total_posts"],
            "training_eligible": verification["training_eligible_image_posts"],
        },
        "analysis": analysis,
        "profile": {
            "version": profile["version"],
            "training_samples": evaluation["training_samples"],
            "holdout_samples": evaluation["holdout_samples"],
            "duplicate_detection": evaluation["duplicate_detection"],
        },
        "discovery": {
            "candidates": discovery["candidates"],
            "accepted": discovery["accepted"],
            "hard_rejected": discovery["hard_rejected"],
        },
        "generation": generation,
        "queue": {"coverage": queue["coverage"], "gaps": queue["gaps"]},
    }
