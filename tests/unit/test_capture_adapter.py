from leeway.capture.adapter import (
    YouTubeCommunityPostsAdapterV1,
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
