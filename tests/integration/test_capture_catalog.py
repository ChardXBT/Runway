from pathlib import Path

import pytest
from sqlalchemy import event, func, select

from runway.capture.schemas import BrowserDomSnapshot, ExtractedPost, ImageReference
from runway.capture.service import CaptureService
from runway.catalog.service import CatalogService
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import CaptureRun, MediaAsset, Post, PostMedia, RawPostRecord
from runway.domain.enums import CaptureMode, PostType


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

    list_queries: list[str] = []

    def record_list_query(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT"):
            list_queries.append(statement)

    event.listen(database.engine, "before_cursor_execute", record_list_query)
    try:
        listed = CatalogService(database, settings).list_posts(limit=500)
    finally:
        event.remove(database.engine, "before_cursor_execute", record_list_query)
    assert len(listed) == 12
    assert len(list_queries) <= 3

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


def test_completed_live_capture_reopens_in_place_on_explicit_resume(
    database: Database, settings: Settings
) -> None:
    capture = CaptureService(database, settings)
    first = ExtractedPost(
        external_post_id="UgkxFirst",
        permalink="https://www.youtube.com/post/UgkxFirst",
        post_type=PostType.IMAGE,
        caption="First live checkpoint.",
        images=[ImageReference(url="fixture://history-01")],
    )
    completed = capture.append_records(
        [first],
        mode=CaptureMode.MANAGED_BROWSER,
        channel_url="https://www.youtube.com/@Qlob/posts",
        finalize=True,
    )
    assert completed.status == "completed"
    assert capture.active_seen_keys(CaptureMode.MANAGED_BROWSER) == {"UgkxFirst"}

    second = ExtractedPost(
        external_post_id="UgkxSecond",
        permalink="https://www.youtube.com/post/UgkxSecond",
        post_type=PostType.IMAGE,
        caption="Second live checkpoint.",
        images=[ImageReference(url="fixture://history-02")],
    )
    resumed = capture.append_records(
        [second],
        mode=CaptureMode.MANAGED_BROWSER,
        channel_url="https://www.youtube.com/@Qlob/posts",
        resume=True,
    )

    assert resumed.run_id == completed.run_id
    assert resumed.status == "paused"
    assert resumed.posts_seen == 2
    assert capture.active_seen_keys(CaptureMode.MANAGED_BROWSER) == {
        "UgkxFirst",
        "UgkxSecond",
    }
    with database.session() as session:
        run = session.get(CaptureRun, resumed.run_id)
        assert run is not None
        assert run.completed_at is None


def test_browser_agent_checkpoint_is_bounded_idempotent_and_exactly_finalized(
    database: Database, settings: Settings
) -> None:
    capture = CaptureService(database, settings)
    html = """
    <ytd-backstage-post-thread-renderer>
      <a href="/post/UgkxBrowserAgent">permalink</a>
      <yt-formatted-string id="content-text">Browser-agent truth.</yt-formatted-string>
      <yt-formatted-string id="published-time-text">2 days ago</yt-formatted-string>
      <ytd-backstage-image-renderer>
        <img src="fixture://history-01">
      </ytd-backstage-image-renderer>
    </ytd-backstage-post-thread-renderer>
    """
    snapshot = BrowserDomSnapshot(
        html=html,
        observed_at="2026-07-17T12:00:00Z",
    )
    checkpoint = capture.append_dom_checkpoint(
        [snapshot],
        channel_url="https://www.youtube.com/channel/test/posts",
        surface_card_count=1,
        surface_tail_key="UgkxBrowserAgent",
    )
    assert checkpoint.posts_seen == 1
    assert checkpoint.status == "paused"

    with pytest.raises(ValueError, match="surface tail"):
        capture.append_dom_checkpoint(
            [snapshot],
            channel_url="https://www.youtube.com/channel/test/posts",
            surface_card_count=1,
            surface_tail_key="UgkxWrongTail",
            finalize=True,
            expected_total=1,
        )

    completed = capture.append_dom_checkpoint(
        [snapshot],
        channel_url="https://www.youtube.com/channel/test/posts",
        surface_card_count=1,
        surface_tail_key="UgkxBrowserAgent",
        finalize=True,
        expected_total=1,
    )
    assert completed.run_id == checkpoint.run_id
    assert completed.posts_seen == 1
    assert completed.status == "completed"
    assert (settings.resolved_data_dir / "captures" / "posts" / "UgkxBrowserAgent.html").is_file()


def test_browser_agent_checkpoint_rejects_non_youtube_sources(
    database: Database, settings: Settings
) -> None:
    capture = CaptureService(database, settings)
    snapshot = BrowserDomSnapshot(
        html="<ytd-backstage-post-thread-renderer />",
        observed_at="2026-07-17T12:00:00Z",
    )
    try:
        capture.append_dom_checkpoint(
            [snapshot],
            channel_url="https://example.com/posts",
            surface_card_count=1,
            surface_tail_key="not-used",
        )
    except ValueError as exc:
        assert "youtube.com" in str(exc)
    else:
        raise AssertionError("non-YouTube browser checkpoint was accepted")


def test_capture_deduplicates_identical_media_references_within_one_post(
    database: Database, settings: Settings
) -> None:
    capture = CaptureService(database, settings)
    duplicate_gallery = ExtractedPost(
        external_post_id="UgkxDuplicateGallery",
        permalink="https://www.youtube.com/post/UgkxDuplicateGallery",
        post_type=PostType.MULTI_IMAGE,
        caption="The same canonical image appears twice.",
        images=[
            ImageReference(url="fixture://history-01"),
            ImageReference(url="fixture://history-01"),
        ],
    )
    duplicate_gallery.raw_dom_snapshot_path = capture.save_post_snapshot(
        duplicate_gallery,
        """
        <ytd-backstage-post-thread-renderer>
          <a href="/post/UgkxDuplicateGallery">permalink</a>
          <yt-formatted-string id="content-text">
            The same canonical image appears twice.
          </yt-formatted-string>
          <ytd-backstage-image-renderer>
            <img src="fixture://history-01">
          </ytd-backstage-image-renderer>
          <ytd-backstage-image-renderer>
            <img src="fixture://history-01">
          </ytd-backstage-image-renderer>
        </ytd-backstage-post-thread-renderer>
        """,
    )

    result = capture.append_records(
        [duplicate_gallery],
        mode=CaptureMode.MANAGED_BROWSER,
        channel_url="https://www.youtube.com/@Qlob/posts",
    )

    assert result.posts_seen == 1
    with database.session() as session:
        post = session.scalar(select(Post).where(Post.external_post_id == "UgkxDuplicateGallery"))
        assert post is not None
        assert post.is_training_eligible is True
        assert (
            session.scalar(
                select(func.count(PostMedia.media_asset_id)).where(PostMedia.post_id == post.id)
            )
            == 1
        )
    reparse = capture.reparse_snapshots(run_id=result.run_id, dry_run=True)
    assert reparse["reparsed"] == 1
    assert reparse["errors"] == []


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
