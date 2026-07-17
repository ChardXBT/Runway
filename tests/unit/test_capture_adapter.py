from datetime import UTC, datetime

from leeway.capture.adapter import (
    YouTubeCommunityPostsAdapterV1,
    canonicalize_youtube_image_url,
    fixture_dom_path,
    parse_visible_count,
)


def test_adapter_covers_sanitized_post_shapes() -> None:
    batch = YouTubeCommunityPostsAdapterV1().extract_file(fixture_dom_path())
    assert len(batch.posts) == 13
    assert len({post.stable_key() for post in batch.posts}) == 12
    assert {post.post_type.value for post in batch.posts} >= {
        "text",
        "image",
        "multi_image",
        "poll",
        "video_share",
    }
    collapsed = next(post for post in batch.posts if post.external_post_id == "qlob-005")
    assert collapsed.caption == "He really thought nobody noticed the dramatic entrance."
    lazy = next(post for post in batch.posts if post.external_post_id == "qlob-008")
    assert lazy.images[0].url == "fixture://history-07"
    srcset = next(post for post in batch.posts if post.external_post_id == "qlob-011")
    assert srcset.images[0].url == "fixture://history-10"
    assert any(item.code == "unexpected_layout" for item in batch.diagnostics)


def test_visible_engagement_count_parser_preserves_unknowns() -> None:
    assert parse_visible_count("1.2K likes") == 1200
    assert parse_visible_count("2M") == 2_000_000
    assert parse_visible_count("hidden") is None


def test_live_youtube_card_excludes_avatar_and_hidden_placeholders() -> None:
    html = """
    <ytd-backstage-post-thread-renderer>
      <a href="/post/UgkxPrimary123">permalink</a>
      <div id="author-thumbnail">
        <img alt="Qlob" src="https://yt3.googleusercontent.com/avatar=s48">
      </div>
      <yt-formatted-string id="published-time-text">4 days ago</yt-formatted-string>
      <yt-formatted-string id="content-text">A complete caption</yt-formatted-string>
      <ytd-backstage-image-renderer>
        <img src="https://yt3.ggpht.com/source-hash=s626-c-fcrop64=1,0fffffffffffffff">
      </ytd-backstage-image-renderer>
      <ytd-backstage-poll-renderer hidden></ytd-backstage-poll-renderer>
      <ytd-backstage-quiz-renderer hidden></ytd-backstage-quiz-renderer>
      <ytd-post-uploaded-video-renderer hidden></ytd-post-uploaded-video-renderer>
      <span id="vote-count-middle">1.2K</span>
      <div id="reply-button-end">
        <a aria-label="34 comments" href="/post/UgkxPrimary123">34</a>
      </div>
    </ytd-backstage-post-thread-renderer>
    """
    observed_at = datetime(2026, 7, 17, 12, tzinfo=UTC)
    batch = YouTubeCommunityPostsAdapterV1().extract_html(
        html,
        observed_at=observed_at,
    )

    assert not batch.diagnostics
    assert len(batch.posts) == 1
    post = batch.posts[0]
    assert post.external_post_id == "UgkxPrimary123"
    assert post.post_type.value == "image"
    assert post.caption == "A complete caption"
    assert post.published_at == datetime(2026, 7, 13, 12, tzinfo=UTC)
    assert post.date_precision.value == "relative"
    assert post.like_count == 1_200
    assert post.comment_count == 34
    assert len(post.images) == 1
    assert post.images[0].url == "https://yt3.ggpht.com/source-hash=s0"
    assert post.images[0].display_url is not None


def test_live_youtube_card_is_deferred_until_lazy_media_has_a_source() -> None:
    html = """
    <ytd-backstage-post-thread-renderer>
      <a href="/post/UgkxPending123">permalink</a>
      <yt-formatted-string id="content-text">Do not store this partially.</yt-formatted-string>
      <ytd-backstage-image-renderer><img alt=""></ytd-backstage-image-renderer>
    </ytd-backstage-post-thread-renderer>
    """
    batch = YouTubeCommunityPostsAdapterV1().extract_html(html)
    assert batch.posts == []
    assert [item.code for item in batch.diagnostics] == ["lazy_media_pending"]


def test_only_youtube_cdn_urls_are_canonicalized() -> None:
    assert (
        canonicalize_youtube_image_url("https://yt3.ggpht.com/hash=s144-c-rw")
        == "https://yt3.ggpht.com/hash=s0"
    )
    external = "https://example.com/image.jpg?size=s144"
    assert canonicalize_youtube_image_url(external) == external


def test_zero_comment_button_is_recorded_as_zero() -> None:
    html = """
    <ytd-backstage-post-thread-renderer>
      <a href="/post/UgkxNoComments">permalink</a>
      <yt-formatted-string id="content-text">No replies yet.</yt-formatted-string>
      <ytd-backstage-image-renderer>
        <img src="https://yt3.ggpht.com/source-hash=s480">
      </ytd-backstage-image-renderer>
      <div id="reply-button-end">
        <a aria-label="Comment" href="/post/UgkxNoComments"></a>
      </div>
    </ytd-backstage-post-thread-renderer>
    """
    post = YouTubeCommunityPostsAdapterV1().extract_html(html).posts[0]
    assert post.comment_count == 0
    assert post.raw_comment_text == "Comment"
