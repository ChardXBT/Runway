from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from runway.analysis.service import AnalysisService
from runway.capture.service import CaptureService
from runway.catalog.service import CatalogService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CaptureRun,
    Channel,
    MediaAsset,
    Post,
    PostMedia,
    RepresentationRecord,
    RetrievalEvidenceRecord,
)
from runway.intelligence.profile import StyleProfileService
from runway.intelligence.retrieval import ReferenceRetrievalQuery, RetrievalService


@pytest.mark.asyncio
async def test_retrieval_persists_considered_selected_and_multi_image_evidence(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    await AnalysisService(database, settings).analyze_history()
    await StyleProfileService(database, settings).build()

    with database.session() as session:
        multi_image_post = session.scalar(
            select(Post).where(Post.external_post_id == "qlob-002")
        )
        assert multi_image_post is not None
        media_ids = list(
            session.scalars(
                select(PostMedia.media_asset_id)
                .where(PostMedia.post_id == multi_image_post.id)
                .order_by(PostMedia.position)
            )
        )
    assert len(media_ids) == 2

    service = RetrievalService(database, settings)
    context = service.context_for_candidate(media_ids[0])
    run_id = int(context["retrieval_run_id"])

    with database.session() as session:
        evidence = session.scalars(
            select(RetrievalEvidenceRecord).where(
                RetrievalEvidenceRecord.retrieval_run_id == run_id
            )
        ).all()
        representation_count = session.scalar(
            select(func.count(RepresentationRecord.id))
        )
    selected = [row for row in evidence if row.selected]
    considered = [row for row in evidence if row.retrieval_channel != "fusion"]
    assert considered
    assert selected
    assert len(considered) > len(selected)
    assert {row.channel_id for row in evidence} == {1}
    assert all(row.evidence_role for row in selected)
    assert sorted(row.selected_rank for row in selected) == list(
        range(1, len(selected) + 1)
    )
    assert representation_count is not None and representation_count > 0
    assert any(
        item["post_id"] == multi_image_post.id
        and item["media_asset_ids"] == media_ids
        for item in context["visual_examples"]  # type: ignore[union-attr]
    )


@pytest.mark.asyncio
async def test_channel_data_isolation_blocks_catalog_and_reference_leakage(
    database: Database,
    settings: Settings,
) -> None:
    CaptureService(database, settings).run_fixture()
    await AnalysisService(database, settings).analyze_history()
    await StyleProfileService(database, settings).build()
    with database.session() as session:
        qlob_media_id = session.scalar(
            select(PostMedia.media_asset_id)
            .join(Post, Post.id == PostMedia.post_id)
            .where(Post.channel_id == 1)
            .limit(1)
        )
        assert qlob_media_id is not None
        source = session.get(MediaAsset, qlob_media_id)
        assert source is not None
        other_channel = Channel(
            name="Quiet Science",
            handle="quiet-science",
            youtube_channel_id="UC-SCIENCE-FIXTURE",
            timezone="Europe/London",
            default_post_time="09:00",
            planning_horizon_days=0,
            duplicate_window_days=180,
        )
        session.add(other_channel)
        session.flush()
        other_capture = CaptureRun(
            channel_id=other_channel.id,
            mode="fixture",
            channel_url="fixture://quiet-science/community",
            status="completed",
            completed_at=datetime.now(UTC),
        )
        session.add(other_capture)
        session.flush()
        other_media = MediaAsset(
            kind="historical",
            local_path=source.local_path,
            original_url="fixture://quiet-science/image-1",
            source_page_url="fixture://quiet-science/post-1",
            source_domain="fixture.local",
            original_filename="quiet-science.png",
            mime_type=source.mime_type,
            width=source.width,
            height=source.height,
            file_size=source.file_size,
            sha256="e" * 64,
            perceptual_hash="d" * 16,
            crop_resistant_hash=None,
            embedding_model=source.embedding_model,
            embedding_vector=source.embedding_vector,
            blur_score=source.blur_score,
            quality_metrics_json=source.quality_metrics_json,
            downloaded_at=datetime.now(UTC),
            rights_status="creator_owned",
        )
        session.add(other_media)
        session.flush()
        other_post = Post(
            channel_id=other_channel.id,
            external_post_id="quiet-science-1",
            permalink="fixture://quiet-science/post-1",
            post_type="image",
            caption="A careful look at the experiment.",
            displayed_date_text="Jul 18, 2026",
            published_at=datetime.now(UTC),
            date_precision="exact",
            like_count=12,
            comment_count=3,
            raw_engagement_json="{}",
            is_published=True,
            is_training_eligible=True,
            source_capture_run_id=other_capture.id,
        )
        session.add(other_post)
        session.flush()
        session.add(
            PostMedia(
                post_id=other_post.id,
                media_asset_id=other_media.id,
                position=0,
            )
        )
        other_media_id = other_media.id

    qlob_catalog = CatalogService(database, settings).list_posts(limit=100)
    other_settings = settings.model_copy(
        update={
            "channel_name": "Quiet Science",
            "channel_handle": "quiet-science",
            "caption_question_first": False,
        }
    )
    other_catalog = CatalogService(database, other_settings).list_posts(limit=100)
    assert all(row["external_post_id"] != "quiet-science-1" for row in qlob_catalog)
    assert [row["external_post_id"] for row in other_catalog] == ["quiet-science-1"]

    with pytest.raises(LookupError, match="outside the configured channel"):
        RetrievalService(database, settings).retrieve_reference(
            ReferenceRetrievalQuery(
                reference_media_ids=[other_media_id],
                modification_text="preserve the desk composition",
            )
        )
    with pytest.raises(LookupError, match="outside the configured channel"):
        RetrievalService(database, other_settings).retrieve_reference(
            ReferenceRetrievalQuery(
                reference_media_ids=[qlob_media_id],
                modification_text="preserve the reaction",
            )
        )
