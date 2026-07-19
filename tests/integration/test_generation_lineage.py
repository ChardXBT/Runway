from __future__ import annotations

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
    assert result["paid_usage"] is False
    assert generation.status == "completed"
    assert generation.provider == "mock"
    assert search_run is not None
    assert search_run.provider == "generated:mock"
    assert candidate.download_status == "generated"
    assert candidate.rights_status == "creator_owned"
    assert lineage is not None
    assert lineage.media_asset_id == candidate.media_asset_id
    assert len(lineage.content_hash) == 64


@pytest.mark.asyncio
async def test_unknown_rights_reference_is_ineligible_for_generation(
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

    with pytest.raises(ValueError, match="unknown-rights media"):
        await ImageGenerationService(database, settings).generate(
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
