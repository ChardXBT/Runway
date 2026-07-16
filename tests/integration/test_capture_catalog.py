from sqlalchemy import func, select

from leeway.capture.service import CaptureService
from leeway.catalog.service import CatalogService
from leeway.config import Settings
from leeway.db.base import Database
from leeway.db.models import MediaAsset, Post


def test_fixture_capture_resumes_and_is_idempotent(database: Database, settings: Settings) -> None:
    capture = CaptureService(database, settings)
    paused = capture.run_fixture(max_posts=4, resume=True)
    assert paused.status == "paused"
    assert paused.cursor == 4

    completed = capture.run_fixture(resume=True)
    assert completed.run_id == paused.run_id
    assert completed.status == "completed"
    assert completed.posts_seen == 12

    with database.session() as session:
        assert session.scalar(select(func.count(Post.id))) == 12
        assert (
            session.scalar(select(func.count(Post.id)).where(Post.is_training_eligible.is_(True)))
            == 9
        )
        asset = session.scalar(select(MediaAsset).order_by(MediaAsset.id).limit(1))
        assert asset is not None
        assert len(asset.sha256) == 64
        assert (settings.resolved_data_dir / asset.local_path).is_file()

    second = capture.run_fixture(resume=True)
    assert second.run_id != completed.run_id
    assert second.posts_created == 0
    with database.session() as session:
        assert session.scalar(select(func.count(Post.id))) == 12


def test_catalog_verification_writes_readable_reports(
    database: Database, settings: Settings
) -> None:
    CaptureService(database, settings).run_fixture()
    report = CatalogService(database, settings).verify()
    assert report["total_posts"] == 12
    assert report["training_eligible_image_posts"] == 9
    assert report["duplicate_external_ids"] == []
    assert len(report["unmatched_raw_records"]) == 1
    assert (settings.resolved_data_dir / "reports" / "catalog-verification.json").is_file()
    assert (settings.resolved_data_dir / "reports" / "catalog-verification.md").is_file()
