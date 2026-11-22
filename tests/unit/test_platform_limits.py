from __future__ import annotations

import dataclasses
from datetime import date

import pytest

from runway.platforms import limits
from runway.platforms.capabilities import Platform

_TODAY = date(2026, 11, 22)
_LABELS = {"official", "community", "unverified", "inference", "decision"}


def _all_limits() -> list[object]:
    return [
        value
        for name, value in vars(limits).items()
        if name.isupper() and dataclasses.is_dataclass(value) and not isinstance(value, type)
    ]


def test_every_limit_carries_source_date_and_label() -> None:
    found = _all_limits()
    assert len(found) >= 25
    for limit in found:
        assert getattr(limit, "source", "").strip(), limit
        read_on = getattr(limit, "read_on", None)
        assert isinstance(read_on, date) and read_on <= _TODAY, limit
        assert getattr(limit, "label", None) in _LABELS, limit


def test_limits_are_frozen() -> None:
    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.INSTAGRAM_REEL.max_size_bytes = 1  # type: ignore[misc]


def test_every_post_type_has_rules_for_the_media_it_accepts() -> None:
    for post_type, platform in limits.POST_TYPE_PLATFORM.items():
        video = limits.VIDEO_LIMITS.get(post_type)
        image = limits.IMAGE_LIMITS.get(post_type)
        assert video is not None or image is not None, post_type
        for rules in (video, image):
            if rules is not None:
                assert rules.post_type == post_type
                assert rules.platform == platform


@pytest.mark.parametrize(
    ("rules", "max_size", "min_ms", "max_ms"),
    [
        (limits.YOUTUBE_VIDEO, 256 * limits.GB, None, None),
        (limits.INSTAGRAM_REEL, 300 * limits.MB, 3_000, 900_000),
        (limits.INSTAGRAM_STORY_VIDEO, 100 * limits.MB, 3_000, 60_000),
        (limits.TIKTOK_VIDEO, 4 * limits.GB, None, 600_000),
    ],
)
def test_video_size_and_duration_match_spec_02_table(
    rules: limits.VideoLimits, max_size: int, min_ms: int | None, max_ms: int | None
) -> None:
    assert rules.max_size_bytes == max_size
    assert rules.min_duration_ms == min_ms
    assert rules.max_duration_ms == max_ms


def test_instagram_and_tiktok_media_rules_match_spec_02_table() -> None:
    reel = limits.INSTAGRAM_REEL
    assert reel.containers == {"mp4", "mov"}
    assert reel.require_moov_at_front and reel.forbid_edit_lists
    assert reel.video_codecs == {"h264", "hevc"}
    assert reel.audio_codecs == {"aac"}
    assert (reel.max_audio_sample_rate_hz, reel.max_audio_channels) == (48_000, 2)
    assert (reel.min_fps, reel.max_fps, reel.max_width) == (23, 60, 1920)
    assert reel.recommended_aspect == pytest.approx(9 / 16)

    tiktok = limits.TIKTOK_VIDEO
    assert tiktok.containers == {"mp4", "webm", "mov"}
    assert tiktok.video_codecs == {"h264", "hevc", "vp8", "vp9"}
    assert (tiktok.min_side, tiktok.max_side) == (360, 4096)

    image = limits.INSTAGRAM_IMAGE
    assert image.formats == {"jpeg"}
    assert image.max_size_bytes == 8 * limits.MB
    assert (image.min_aspect, image.max_aspect) == (pytest.approx(0.8), 1.91)


def test_text_and_daily_limits() -> None:
    assert limits.INSTAGRAM_CAPTION.max_length == 2_200
    assert limits.INSTAGRAM_CAPTION.max_hashtags == 30
    assert limits.INSTAGRAM_CAPTION.max_mentions == 20
    assert limits.TIKTOK_TITLE.unit == "utf16_code_units"
    assert limits.YOUTUBE_TITLE.max_length == 100
    assert limits.YOUTUBE_DESCRIPTION.max_length == 5_000
    by_platform = {(rule.platform, rule.name): rule for rule in limits.DAILY_PUBLISH_LIMITS}
    assert by_platform[(Platform.youtube, "videos.insert")].max_count == 100
    assert by_platform[(Platform.instagram, "media_publish")].max_count == 50


def test_tiktok_chunk_bounds_hold_under_decimal_and_binary_readings() -> None:
    rules = limits.TIKTOK_CHUNKS
    assert rules.min_chunk_bytes >= max(5 * limits.MB, 5 * limits.MiB)
    assert rules.max_chunk_bytes <= min(64 * limits.MB, 64 * limits.MiB)
    assert rules.max_final_chunk_bytes <= min(128 * limits.MB, 128 * limits.MiB)
    assert rules.min_chunk_bytes <= rules.default_chunk_bytes <= rules.max_chunk_bytes
    assert "tiktok.md" in rules.source


def test_youtube_default_chunk_is_a_multiple_of_256_kib() -> None:
    rules = limits.YOUTUBE_UPLOAD
    assert rules.default_chunk_bytes == 8 * limits.MiB
    assert rules.default_chunk_bytes % rules.chunk_multiple_bytes == 0
