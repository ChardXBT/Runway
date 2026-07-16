from leeway.capture.service import CaptureService
from leeway.config import Settings
from leeway.db.base import Database
from leeway.media.service import ensure_fixture_images, inspect_image
from leeway.ranking.duplicates import DuplicateDetector


def test_exact_transformed_and_novel_images_are_distinguished(
    database: Database, settings: Settings
) -> None:
    CaptureService(database, settings).run_fixture()
    assets = ensure_fixture_images(settings)
    detector = DuplicateDetector(database, settings)

    exact = detector.inspect(inspect_image(assets["history-01"]))
    transformed = detector.inspect(inspect_image(assets["history-01-bordered"]))
    novel = detector.inspect(inspect_image(assets["candidate-02"]))

    assert exact.is_exact and exact.hard_block
    assert transformed.is_transformed_duplicate and transformed.hard_block
    assert not novel.hard_block


def test_same_source_url_is_a_hard_block(database: Database, settings: Settings) -> None:
    CaptureService(database, settings).run_fixture()
    assets = ensure_fixture_images(settings)
    result = DuplicateDetector(database, settings).inspect(
        inspect_image(assets["candidate-03"]), source_url="fixture://history-01"
    )
    assert result.hard_block
    assert "source URL was already used" in result.warnings
