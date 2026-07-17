from pathlib import Path

from sqlalchemy import func, select

from leeway.capture.schemas import ExtractedPost, ImageReference
from leeway.capture.service import CaptureService
from leeway.catalog.service import CatalogService
from leeway.config import Settings
from leeway.db.base import Database
from leeway.db.models import MediaAsset, Post, RawPostRecord
from leeway.domain.enums import CaptureMode, PostType


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


def test_live_checkpoint_retries_media_before_advancing_cursor(
    database: Database, settings: Settings
) -> None:
    capture = CaptureService(database, settings)
    snapshot = Path("captures/posts/UgkxRetry.html").as_posix()
    incomplete = ExtractedPost(
        external_post_id="UgkxRetry",
        permalink="https://www.youtube.com/post/UgkxRetry",
        post_type=PostType.IMAGE,
        caption="Retry this media safely.",
        images=[ImageReference(url="fixture://missing")],
        raw_dom_snapshot_path=snapshot,
    )

    first = capture.append_records(
        [incomplete],
        mode=CaptureMode.MANAGED_BROWSER,
        channel_url="https://www.youtube.com/@Qlob/posts",
    )
    assert first.posts_seen == 0
    assert capture.active_seen_keys(CaptureMode.MANAGED_BROWSER) == set()

    complete = incomplete.model_copy(
        update={"images": [ImageReference(url="fixture://history-01")]}
    )
    second = capture.append_records(
        [complete],
        mode=CaptureMode.MANAGED_BROWSER,
        channel_url="https://www.youtube.com/@Qlob/posts",
    )
    assert second.run_id == first.run_id
    assert second.posts_seen == 1
    assert capture.active_seen_keys(CaptureMode.MANAGED_BROWSER) == {"UgkxRetry"}
    with database.session() as session:
        raw = session.scalar(select(RawPostRecord).where(RawPostRecord.record_key == "UgkxRetry"))
        assert raw is not None
        assert raw.raw_dom_snapshot_path == snapshot


def test_snapshot_reparse_repairs_normalized_engagement(
    database: Database, settings: Settings
) -> None:
    capture = CaptureService(database, settings)
    snapshot = Path("captures/posts/UgkxReparse.html")
    snapshot_path = settings.resolved_data_dir / snapshot
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        """
        <ytd-backstage-post-thread-renderer>
          <a href="/post/UgkxReparse">permalink</a>
          <yt-formatted-string id="content-text">Snapshot truth.</yt-formatted-string>
          <yt-formatted-string id="published-time-text">2 days ago</yt-formatted-string>
          <ytd-backstage-image-renderer>
            <img src="fixture://history-01">
          </ytd-backstage-image-renderer>
          <span id="vote-count-middle">45</span>
          <div id="reply-button-end">
            <a aria-label="7 comments" href="/post/UgkxReparse">7</a>
          </div>
        </ytd-backstage-post-thread-renderer>
        """,
        encoding="utf-8",
    )
    initial = ExtractedPost(
        external_post_id="UgkxReparse",
        permalink="https://www.youtube.com/post/UgkxReparse",
        post_type=PostType.IMAGE,
        caption="Snapshot truth.",
        like_count=45,
        comment_count=None,
        images=[ImageReference(url="fixture://history-01")],
        raw_dom_snapshot_path=snapshot.as_posix(),
    )
    checkpoint = capture.append_records(
        [initial],
        mode=CaptureMode.MANAGED_BROWSER,
        channel_url="https://www.youtube.com/@Qlob/posts",
    )

    result = capture.reparse_snapshots(run_id=checkpoint.run_id)
    assert result["reparsed"] == 1
    assert result["errors"] == []
    with database.session() as session:
        post = session.scalar(select(Post).where(Post.external_post_id == "UgkxReparse"))
        assert post is not None
        assert post.comment_count == 7
        assert post.like_count == 45
