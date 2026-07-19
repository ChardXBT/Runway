from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from runway.analysis.service import AnalysisService
from runway.capture.service import CaptureService
from runway.config import Settings
from runway.db.base import Database
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
from runway.generation.schemas import CreativeBrief
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
