from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from runway.analysis.service import AnalysisService
from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db.base import Database
from runway.db.job_leases import LeaseLost, lease_claim_fields, new_owner_token
from runway.db.models import (
    CandidateImage,
    GeneratedAssetLineage,
    ImageGenerationRun,
    IntelligenceAgentRun,
    MediaAsset,
    Post,
    PostMedia,
    SearchRun,
)
from runway.db.repositories import get_channel
from runway.discovery.recovery import recover_stale_search_runs
from runway.generation.providers import DeterministicMockImageProvider
from runway.generation.schemas import CreativeBrief, ProviderRequest
from runway.generation.service import ImageGenerationService
from runway.intelligence.profile import StyleProfileService


@pytest.mark.asyncio
async def test_mock_image_generation_preserves_lineage_and_reenters_candidates(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    await AnalysisService(database, settings).analyze_history()
    await StyleProfileService(database, settings).build()
    service = ImageGenerationService(database, settings)

    result = await service.generate(
        CreativeBrief(
            capability="text_to_image",
            instruction="A focused person examining a red object at a desk",
            negative_instruction="No text overlay or watermark",
            seed=20260718,
            width=640,
            height=640,
        ),
        provider_name="mock",
        output_count=1,
    )

    candidate_id = int(result["candidate_ids"][0])  # type: ignore[index]
    with database.session() as session:
        generation = session.get(
            ImageGenerationRun,
            int(result["generation_run_id"]),
        )
        candidate = session.get(CandidateImage, candidate_id)
        assert generation is not None
        assert candidate is not None
        search_run = session.get(SearchRun, candidate.search_run_id)
        lineage = session.scalar(
            select(GeneratedAssetLineage).where(
                GeneratedAssetLineage.generation_run_id == generation.id
            )
        )
        agent_run = session.get(IntelligenceAgentRun, generation.agent_run_id)
    assert result["paid_usage"] is False
    assert generation.status == "completed"
    assert generation.provider == "mock"
    assert agent_run is not None
    assert agent_run.status == "completed"
    assert agent_run.capability == "image_generation_mock"
    assert search_run is not None
    assert search_run.provider == "generated:mock"
    assert candidate.download_status == "generated"
    assert candidate.rights_status == "creator_owned"
    assert lineage is not None
    assert lineage.media_asset_id == candidate.media_asset_id
    assert lineage.candidate_image_id == candidate.id
    assert lineage.review_status == "pending"
    assert len(lineage.content_hash) == 64


@pytest.mark.asyncio
async def test_image_generation_entrypoint_closes_crashed_workspace_and_completes_owned_run(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    await AnalysisService(database, settings).analyze_history()
    await StyleProfileService(database, settings).build()
    now = datetime.now(UTC)
    crashed_owner = new_owner_token()
    crashed_lease = lease_claim_fields(
        crashed_owner,
        lease_duration=timedelta(minutes=5),
        now=now - timedelta(minutes=10),
    )
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
        crashed_generation = ImageGenerationRun(
            channel_id=channel_id,
            provider="mock",
            model="mock-image",
            model_version="1",
            capability="text_to_image",
            creative_brief_json="{}",
            status="running",
            started_at=now - timedelta(minutes=10),
        )
        session.add(crashed_generation)
        session.flush()
        crashed_generation_id = crashed_generation.id
        crashed_search = SearchRun(
            channel_id=channel_id,
            style_profile_id=None,
            query_plan_json=json.dumps(
                {
                    "source": "image_generation",
                    "generation_run_id": crashed_generation_id,
                }
            ),
            provider="generated:mock",
            status="running",
            started_at=now - timedelta(minutes=10),
            owner_token=crashed_owner,
            heartbeat_at=crashed_lease["heartbeat_at"],
            lease_expires_at=crashed_lease["lease_expires_at"],
            lease_version=crashed_lease["lease_version"],
        )
        session.add(crashed_search)
        session.flush()
        crashed_search_id = crashed_search.id

    result = await ImageGenerationService(database, settings).generate(
        CreativeBrief(
            capability="text_to_image",
            instruction="A focused person examining a red object at a desk",
            seed=20260719,
            width=640,
            height=640,
        ),
        provider_name="mock",
        output_count=1,
    )

    assert result["status"] == "completed"
    with database.session() as session:
        old_generation = session.get(ImageGenerationRun, crashed_generation_id)
        old_search = session.get(SearchRun, crashed_search_id)
        new_generation = session.get(ImageGenerationRun, int(result["generation_run_id"]))
        new_search = session.get(SearchRun, int(result["search_run_id"]))
        assert old_generation is not None
        assert old_generation.status == "failed"
        assert "without a valid lease" in str(old_generation.error_summary)
        assert old_generation.completed_at is not None
        assert old_search is not None
        assert old_search.status == "failed"
        assert "without a valid lease" in str(old_search.error_summary)
        assert new_generation is not None
        assert new_generation.status == "completed"
        assert new_search is not None
        assert new_search.status == "completed"
        assert new_search.owner_token
        assert new_search.heartbeat_at is not None
        assert new_search.lease_expires_at is not None


@pytest.mark.asyncio
async def test_image_generation_late_failure_cannot_overwrite_recovery_evidence(
    database: Database,
    settings: Settings,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingFailingProvider(DeterministicMockImageProvider):
        name = "blocking-failing"

        async def generate(self, request: ProviderRequest):
            del request
            started.set()
            await release.wait()
            raise RuntimeError("provider exploded")

    provider = BlockingFailingProvider()

    class Registry:
        def get(self, name: str):
            assert name == provider.name
            return provider

    service = ImageGenerationService(database, settings, registry=Registry())
    task = asyncio.create_task(
        service.generate(
            CreativeBrief(
                capability="text_to_image",
                instruction="A focused person examining a red object at a desk",
                seed=20260720,
                width=640,
                height=640,
            ),
            provider_name=provider.name,
            output_count=1,
        )
    )
    await asyncio.wait_for(started.wait(), timeout=5)
    with database.session() as session:
        search = session.scalar(
            select(SearchRun)
            .where(SearchRun.provider == f"generated:{provider.name}")
            .order_by(SearchRun.id.desc())
            .limit(1)
        )
        assert search is not None
        search.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        search.heartbeat_at = datetime.now(UTC) - timedelta(minutes=1)
        search_id = search.id
        generation_run_id = json.loads(search.query_plan_json)["generation_run_id"]

    assert recover_stale_search_runs(database, settings) == 1
    release.set()
    with pytest.raises(LeaseLost):
        await task

    with database.session() as session:
        search = session.get(SearchRun, search_id)
        generation = session.get(ImageGenerationRun, generation_run_id)
        assert search is not None
        assert search.status == "failed"
        assert "without a valid lease" in str(search.error_summary)
        assert generation is not None
        assert generation.status == "failed"
        assert "without a valid lease" in str(generation.error_summary)
        assert "provider exploded" not in str(generation.error_summary)


@pytest.mark.asyncio
async def test_unknown_rights_reference_is_allowed_with_provenance(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    with database.session() as session:
        reference_id = session.scalar(
            select(PostMedia.media_asset_id)
            .join(Post, Post.id == PostMedia.post_id)
            .where(Post.channel_id == 1)
            .limit(1)
        )
        reference = session.get(MediaAsset, reference_id)
        assert reference is not None
        reference.rights_status = "unknown"
    assert reference_id is not None

    result = await ImageGenerationService(database, settings).generate(
        CreativeBrief(
            capability="variation",
            instruction="Create a restrained variation of this composition",
            reference_media_ids=[reference_id],
            consent_state="creator_confirmed",
            seed=10,
            width=640,
            height=640,
        )
    )

    with database.session() as session:
        generation = session.get(
            ImageGenerationRun,
            int(result["generation_run_id"]),
        )
        assert generation is not None
        reference_rights = json.loads(generation.reference_rights_json)
    assert len(reference_rights) == 1
    assert reference_rights[0]["media_asset_id"] == reference_id
    assert reference_rights[0]["rights_status"] == "unknown"
    assert reference_rights[0]["decision"]["outcome"] == "allowed"
    assert reference_rights[0]["decision"]["reason"] == "unknown retained as provenance only"
