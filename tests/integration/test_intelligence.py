import copy
import math

import pytest
from sqlalchemy import event, func, select

from runway.analysis.service import AnalysisService, image_matrix_for_posts
from runway.capture.service import CaptureService
from runway.catalog.service import CatalogService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import MediaAsset, ModelRun, PostAnnotation
from runway.intelligence.profile import StyleProfileService
from runway.intelligence.retrieval import RetrievalService


@pytest.mark.asyncio
async def test_analysis_correction_and_similarity_history(
    database: Database, settings: Settings
) -> None:
    CaptureService(database, settings).run_fixture()
    analysis = AnalysisService(database, settings)
    result = await analysis.analyze_history()
    assert result["completed"] == 9
    assert result["edges"] == 36

    statements: list[str] = []

    def record_statement(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        statements.append(statement)

    event.listen(database.engine, "before_cursor_execute", record_statement)
    try:
        resumed = await analysis.analyze_history()
        image_matrix, matched_ids = image_matrix_for_posts(database, list(range(1, 10)))
    finally:
        event.remove(database.engine, "before_cursor_execute", record_statement)

    assert resumed["completed"] == 0
    assert resumed["edges"] == 36
    assert not any("DELETE FROM similarity_edges" in value for value in statements)
    assert not any("INSERT INTO similarity_edges" in value for value in statements)
    assert sum("FROM post_media" in value for value in statements) == 1
    assert image_matrix.shape[0] == len(matched_ids)
    assert matched_ids

    before = analysis.effective_annotation(1)
    corrected = analysis.correct_annotation(1, {"franchise": "Human reviewed fixture"})
    assert corrected["effective"]["franchise"] == "Human reviewed fixture"
    assert corrected["original"] == before["original"]

    refreshed = await analysis.analyze_history(resume=False, max_posts=1)
    assert refreshed["completed"] == 1
    assert analysis.effective_annotation(1)["effective"]["franchise"] == ("Human reviewed fixture")

    with database.session() as session:
        assert session.scalar(select(func.count(PostAnnotation.id))) == 9
        assert session.scalar(select(func.count(ModelRun.id))) == (
            math.ceil(9 / settings.analysis_batch_size) + 1
        )

    catalog = CatalogService(database, settings)
    assert catalog.set_training_eligibility(1, False)["is_training_eligible"] is False
    assert analysis._similarity_edge_count() == 28
    assert catalog.set_training_eligibility(1, True)["is_training_eligible"] is True
    assert analysis._similarity_edge_count() == 36


@pytest.mark.asyncio
async def test_partial_first_analysis_repairs_the_complete_similarity_graph(
    database: Database, settings: Settings
) -> None:
    CaptureService(database, settings).run_fixture()

    result = await AnalysisService(database, settings).analyze_history(max_posts=1)

    assert result["completed"] == 1
    assert result["deferred"] == 8
    assert result["edges"] == 36


@pytest.mark.asyncio
async def test_profile_is_reproducible_and_evaluation_is_measured(
    database: Database, settings: Settings
) -> None:
    CaptureService(database, settings).run_fixture()
    analysis = AnalysisService(database, settings)
    await analysis.analyze_history()
    raw_post_two = analysis.effective_annotation(2)
    raw_franchise = raw_post_two["effective"]["franchise"]
    analysis.correct_annotation(2, {"franchise": "Reviewed profile franchise"})
    profiles = StyleProfileService(database, settings)
    first = await profiles.build()
    second = await profiles.build()
    first_normalized = copy.deepcopy(first)
    second_normalized = copy.deepcopy(second)
    first_normalized.pop("version")
    second_normalized.pop("version")
    assert first_normalized == second_normalized
    assert second["holdout_post_ids"]
    distribution = dict(second["franchise_distribution"])
    assert distribution["Reviewed profile franchise"] == 1
    expected_raw_franchise_count = sum(
        analysis.effective_annotation(int(post_id))["effective"]["franchise"] == raw_franchise
        for post_id in second["training_post_ids"]
    )
    assert expected_raw_franchise_count > 0
    assert distribution.get(raw_franchise, 0) == expected_raw_franchise_count
    assert second["reviewed_training_annotation_post_ids"] == [2]
    assert second["reviewed_catalogue_annotation_post_ids"] == [2]
    assert second["corrected_training_annotation_post_ids"] == [2]
    assert sum(count for _name, count in second["dominant_caption_structures"]) == 7

    evaluation = profiles.evaluate()
    assert evaluation["training_samples"] + evaluation["holdout_samples"] == 9
    assert 0 <= evaluation["image_caption_matching"]["accuracy"] <= 1
    assert 0 <= evaluation["duplicate_detection"]["transformed_true_positive_rate"] <= 1
    assert (settings.resolved_data_dir / "reports" / "profile-evaluation.md").is_file()

    with database.session() as session:
        media_id = session.scalar(select(MediaAsset.id).order_by(MediaAsset.id).limit(1))
        first_two = session.scalars(select(MediaAsset).order_by(MediaAsset.id).limit(2)).all()
        assert len(first_two) == 2
        first_two[1].embedding_vector = first_two[0].embedding_vector
    assert media_id is not None
    context = RetrievalService(database, settings).context_for_candidate(media_id)
    assert len(context["visual_examples"]) <= 8
    assert len(context["caption_style_examples"]) <= 8
    assert context["style_profile_version"] == 2
