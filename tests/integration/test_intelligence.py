import copy

import pytest
from sqlalchemy import func, select

from leeway.analysis.service import AnalysisService
from leeway.capture.service import CaptureService
from leeway.config import Settings
from leeway.db.base import Database
from leeway.db.models import MediaAsset, ModelRun, PostAnnotation
from leeway.intelligence.profile import StyleProfileService
from leeway.intelligence.retrieval import RetrievalService


@pytest.mark.asyncio
async def test_analysis_correction_and_similarity_history(
    database: Database, settings: Settings
) -> None:
    CaptureService(database, settings).run_fixture()
    analysis = AnalysisService(database, settings)
    result = await analysis.analyze_history()
    assert result["completed"] == 9
    assert result["edges"] == 36

    before = analysis.effective_annotation(1)
    corrected = analysis.correct_annotation(1, {"franchise": "Human reviewed fixture"})
    assert corrected["effective"]["franchise"] == "Human reviewed fixture"
    assert corrected["original"] == before["original"]

    with database.session() as session:
        assert session.scalar(select(func.count(PostAnnotation.id))) == 9
        assert session.scalar(select(func.count(ModelRun.id))) == 9


@pytest.mark.asyncio
async def test_profile_is_reproducible_and_evaluation_is_measured(
    database: Database, settings: Settings
) -> None:
    CaptureService(database, settings).run_fixture()
    await AnalysisService(database, settings).analyze_history()
    profiles = StyleProfileService(database, settings)
    first = await profiles.build()
    second = await profiles.build()
    first_normalized = copy.deepcopy(first)
    second_normalized = copy.deepcopy(second)
    first_normalized.pop("version")
    second_normalized.pop("version")
    assert first_normalized == second_normalized
    assert second["holdout_post_ids"]

    evaluation = profiles.evaluate()
    assert evaluation["training_samples"] + evaluation["holdout_samples"] == 9
    assert 0 <= evaluation["image_caption_matching"]["accuracy"] <= 1
    assert 0 <= evaluation["duplicate_detection"]["transformed_true_positive_rate"] <= 1
    assert (settings.resolved_data_dir / "reports" / "profile-evaluation.md").is_file()

    with database.session() as session:
        media_id = session.scalar(select(MediaAsset.id).order_by(MediaAsset.id).limit(1))
    assert media_id is not None
    context = RetrievalService(database, settings).context_for_candidate(media_id)
    assert len(context["visual_examples"]) <= 8
    assert len(context["caption_style_examples"]) <= 8
    assert context["style_profile_version"] == 2
