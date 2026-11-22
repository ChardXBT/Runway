"""Validation against every row of the spec 02 §5 table, per target column."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from runway.mediastore import Issue, ProbeResult, parse_ffprobe, validate, validate_carousel
from runway.platforms.capabilities import Platform
from runway.platforms.limits import GB, MB, POST_TYPE_PLATFORM, PostType

FIXTURES = Path(__file__).parent / "fixtures" / "ffprobe"

VIDEO = ProbeResult(
    kind="video",
    container="mp4",
    format_names=("mov", "mp4", "m4a", "3gp", "3g2", "mj2"),
    size_bytes=50 * MB,
    duration_ms=30_000,
    bitrate=10_000_000,
    video_codec="h264",
    width=1080,
    height=1920,
    rotation=0,
    fps=30.0,
    audio_codec="aac",
    audio_sample_rate=48_000,
    audio_channels=2,
    moov_at_front=True,
    has_edit_list=False,
)
IMAGE = replace(
    VIDEO,
    kind="image",
    container="jpeg",
    format_names=("jpeg_pipe",),
    size_bytes=2 * MB,
    duration_ms=None,
    bitrate=None,
    video_codec="mjpeg",
    width=1080,
    height=1350,
    fps=None,
    audio_codec=None,
    audio_sample_rate=None,
    audio_channels=None,
    moov_at_front=None,
    has_edit_list=None,
)
VIDEO_TYPES: list[PostType] = [
    "youtube_video",
    "youtube_short",
    "instagram_reel",
    "instagram_story",
    "tiktok_video",
]


def _check(probe: ProbeResult, post_type: PostType, **options: Any) -> list[Issue]:
    return validate(probe, POST_TYPE_PLATFORM[post_type], post_type, **options)


def _codes(issues: list[Issue], severity: str | None = None) -> set[str]:
    return {issue.code for issue in issues if severity is None or issue.severity == severity}


def _probe(base: ProbeResult, changes: dict[str, Any]) -> ProbeResult:
    return replace(base, **changes)


@pytest.mark.parametrize("post_type", VIDEO_TYPES + ["instagram_carousel"])
def test_a_typical_vertical_h264_clip_is_valid_everywhere(post_type: PostType) -> None:
    story = replace(VIDEO, duration_ms=15_000)
    assert _check(story, post_type) == []


@pytest.mark.parametrize("post_type", ["instagram_image", "instagram_carousel"])
def test_a_typical_jpeg_is_valid_for_instagram(post_type: PostType) -> None:
    assert _check(IMAGE, post_type) == []


# (row, post type, probe changes, expected issue code or None for "no issue")
VIDEO_ROWS: list[tuple[str, PostType, dict[str, Any], str | None]] = [
    # Max size
    ("size", "youtube_video", {"size_bytes": 256 * GB}, None),
    ("size", "youtube_video", {"size_bytes": 256 * GB + 1}, "file_too_large"),
    ("size", "instagram_reel", {"size_bytes": 300 * MB}, None),
    ("size", "instagram_reel", {"size_bytes": 300 * MB + 1}, "file_too_large"),
    (
        "size",
        "instagram_story",
        {"size_bytes": 100 * MB + 1, "duration_ms": 15_000},
        "file_too_large",
    ),
    ("size", "tiktok_video", {"size_bytes": 4 * GB}, None),
    ("size", "tiktok_video", {"size_bytes": 4 * GB + 1}, "file_too_large"),
    # Duration
    ("duration", "youtube_video", {"duration_ms": 12 * 3600_000}, None),
    ("duration", "instagram_reel", {"duration_ms": 3_000}, None),
    ("duration", "instagram_reel", {"duration_ms": 2_999}, "duration_too_short"),
    ("duration", "instagram_reel", {"duration_ms": 15 * 60_000}, None),
    ("duration", "instagram_reel", {"duration_ms": 15 * 60_000 + 1}, "duration_too_long"),
    ("duration", "instagram_story", {"duration_ms": 2_000}, "duration_too_short"),
    ("duration", "instagram_story", {"duration_ms": 60_000}, None),
    ("duration", "instagram_story", {"duration_ms": 60_001}, "duration_too_long"),
    ("duration", "tiktok_video", {"duration_ms": 600_000}, None),
    ("duration", "tiktok_video", {"duration_ms": 600_001}, "duration_too_long"),
    ("duration", "instagram_reel", {"duration_ms": None}, "probe_incomplete"),
    # Container
    ("container", "youtube_video", {"container": "matroska"}, None),
    ("container", "youtube_video", {"container": "avi", "moov_at_front": None}, None),
    ("container", "instagram_reel", {"container": "mov"}, None),
    (
        "container",
        "instagram_reel",
        {"container": "webm", "moov_at_front": None},
        "container_not_supported",
    ),
    (
        "container",
        "instagram_story",
        {"container": "matroska", "duration_ms": 15_000},
        "container_not_supported",
    ),
    ("container", "tiktok_video", {"container": "webm"}, None),
    ("container", "tiktok_video", {"container": "mov"}, None),
    ("container", "tiktok_video", {"container": "matroska"}, "container_not_supported"),
    ("moov", "instagram_reel", {"moov_at_front": False}, "moov_not_at_front"),
    (
        "moov",
        "instagram_story",
        {"moov_at_front": False, "duration_ms": 15_000},
        "moov_not_at_front",
    ),
    ("moov", "instagram_reel", {"moov_at_front": None}, "probe_incomplete"),
    ("moov", "tiktok_video", {"moov_at_front": False}, None),
    ("moov", "youtube_video", {"moov_at_front": False}, None),
    ("edit list", "instagram_reel", {"has_edit_list": True}, "edit_list_present"),
    (
        "edit list",
        "instagram_story",
        {"has_edit_list": True, "duration_ms": 15_000},
        "edit_list_present",
    ),
    ("edit list", "tiktok_video", {"has_edit_list": True}, None),
    # Video codec
    ("video codec", "youtube_video", {"video_codec": "av1"}, None),
    ("video codec", "instagram_reel", {"video_codec": "hevc"}, None),
    ("video codec", "instagram_reel", {"video_codec": "vp9"}, "video_codec_not_supported"),
    (
        "video codec",
        "instagram_story",
        {"video_codec": "av1", "duration_ms": 15_000},
        "video_codec_not_supported",
    ),
    ("video codec", "tiktok_video", {"video_codec": "vp8"}, None),
    ("video codec", "tiktok_video", {"video_codec": "av1"}, "video_codec_not_supported"),
    # Audio
    (
        "audio",
        "instagram_reel",
        {"audio_codec": None, "audio_sample_rate": None, "audio_channels": None},
        None,
    ),
    ("audio", "instagram_reel", {"audio_codec": "opus"}, "audio_codec_not_supported"),
    ("audio", "instagram_reel", {"audio_sample_rate": 96_000}, "audio_sample_rate_too_high"),
    ("audio", "instagram_reel", {"audio_channels": 6}, "audio_channels_too_many"),
    (
        "audio",
        "instagram_story",
        {"audio_channels": 6, "duration_ms": 15_000},
        "audio_channels_too_many",
    ),
    ("audio", "tiktok_video", {"audio_codec": "opus", "audio_channels": 6}, None),
    ("audio", "youtube_video", {"audio_codec": "flac", "audio_sample_rate": 192_000}, None),
    # Frame rate
    ("frame rate", "youtube_video", {"fps": 120.0}, None),
    ("frame rate", "instagram_reel", {"fps": 23.0}, None),
    ("frame rate", "instagram_reel", {"fps": 20.0}, "frame_rate_out_of_range"),
    ("frame rate", "instagram_reel", {"fps": 60.0}, None),
    (
        "frame rate",
        "instagram_story",
        {"fps": 120.0, "duration_ms": 15_000},
        "frame_rate_out_of_range",
    ),
    ("frame rate", "tiktok_video", {"fps": 15.0}, "frame_rate_out_of_range"),
    ("frame rate", "tiktok_video", {"fps": 59.94}, None),
    ("frame rate", "tiktok_video", {"fps": None}, "probe_incomplete"),
    # Dimensions
    ("dimensions", "youtube_video", {"width": 7680, "height": 4320}, None),
    ("dimensions", "instagram_reel", {"width": 1920, "height": 3413}, None),
    ("dimensions", "instagram_reel", {"width": 2160, "height": 3840}, "width_too_large"),
    (
        "dimensions",
        "instagram_story",
        {"width": 2160, "height": 3840, "duration_ms": 15_000},
        "width_too_large",
    ),
    ("dimensions", "tiktok_video", {"width": 360, "height": 640}, None),
    ("dimensions", "tiktok_video", {"width": 4096, "height": 2160}, None),
    ("dimensions", "tiktok_video", {"width": 300, "height": 640}, "dimensions_out_of_range"),
    ("dimensions", "tiktok_video", {"width": 2160, "height": 4100}, "dimensions_out_of_range"),
    # Aspect
    ("aspect", "youtube_video", {"width": 1920, "height": 1080}, None),
    ("aspect", "youtube_short", {"width": 1080, "height": 1080}, None),
    ("aspect", "youtube_short", {"width": 1920, "height": 1080}, "short_not_vertical"),
    ("aspect", "instagram_reel", {"width": 1080, "height": 1916}, None),
    ("aspect", "instagram_reel", {"width": 1920, "height": 1080}, "aspect_ratio_not_recommended"),
    ("aspect", "instagram_reel", {"width": 1100, "height": 100}, "aspect_ratio_out_of_range"),
    (
        "aspect",
        "instagram_story",
        {"width": 1080, "height": 1080, "duration_ms": 15_000},
        "aspect_ratio_not_recommended",
    ),
    (
        "aspect",
        "instagram_story",
        {"width": 100, "height": 1100, "duration_ms": 15_000},
        "aspect_ratio_out_of_range",
    ),
    ("aspect", "tiktok_video", {"width": 1920, "height": 1080}, None),
    # YouTube Shorts length heuristic and Instagram bitrate guidance
    ("shorts length", "youtube_short", {"duration_ms": 180_000}, None),
    ("shorts length", "youtube_short", {"duration_ms": 180_001}, "short_too_long"),
    ("bitrate", "instagram_reel", {"bitrate": 30_000_000}, "bitrate_above_recommended"),
]

_WARNINGS = {
    "probe_incomplete",
    "short_not_vertical",
    "short_too_long",
    "aspect_ratio_not_recommended",
    "bitrate_above_recommended",
}


@pytest.mark.parametrize(
    ("row", "post_type", "changes", "expected"),
    VIDEO_ROWS,
    ids=[f"{row}-{post_type}-{expected or 'ok'}" for row, post_type, _, expected in VIDEO_ROWS],
)
def test_video_rows(
    row: str, post_type: PostType, changes: dict[str, Any], expected: str | None
) -> None:
    issues = _check(_probe(VIDEO, changes), post_type)
    if expected is None:
        assert issues == []
        return
    assert _codes(issues) == {expected}, issues
    severity = "warning" if expected in _WARNINGS else "error"
    assert all(issue.severity == severity for issue in issues)
    assert all(issue.source for issue in issues)


def test_moov_and_edit_list_errors_name_the_ffmpeg_fix() -> None:
    issues = _check(
        _probe(VIDEO, {"moov_at_front": False, "has_edit_list": True}), "instagram_reel"
    )
    fixes = {issue.code: issue.fix for issue in issues}
    assert fixes["moov_not_at_front"] is not None
    assert "-movflags +faststart" in fixes["moov_not_at_front"]
    assert fixes["edit_list_present"] is not None
    assert "-use_editlist 0" in fixes["edit_list_present"]
    assert "-movflags +faststart" in fixes["edit_list_present"]


def test_messages_state_the_value_and_the_limit() -> None:
    [issue] = _check(_probe(VIDEO, {"size_bytes": 412 * MB}), "instagram_reel")
    assert issue.message == "The file is 412 MB; Instagram Reels accept at most 300 MB."
    [issue] = _check(_probe(VIDEO, {"video_codec": "vp9"}), "instagram_reel")
    assert "VP9" in issue.message and "H.264" in issue.message and "HEVC" in issue.message


def test_tiktok_creator_duration_limit() -> None:
    probe = _probe(VIDEO, {"duration_ms": 90_000})
    assert _check(probe, "tiktok_video", creator_max_duration_s=120) == []
    [issue] = _check(probe, "tiktok_video", creator_max_duration_s=60)
    assert (issue.severity, issue.code) == ("error", "duration_exceeds_creator_limit")


def test_shorts_warnings_are_labelled_heuristic() -> None:
    issues = _check(
        _probe(VIDEO, {"width": 1920, "height": 1080, "duration_ms": 200_000}), "youtube_short"
    )
    assert _codes(issues, "warning") == {"short_not_vertical", "short_too_long"}
    assert all("Heuristic" in issue.message for issue in issues)


# Images ------------------------------------------------------------------------------------

IMAGE_ROWS: list[tuple[PostType, dict[str, Any], str | None]] = [
    ("instagram_image", {"container": "png", "video_codec": "png"}, "image_format_not_supported"),
    ("instagram_image", {"container": "webp"}, "image_format_not_supported"),
    ("instagram_image", {"size_bytes": 8 * MB}, None),
    ("instagram_image", {"size_bytes": 8 * MB + 1}, "file_too_large"),
    ("instagram_image", {"width": 1080, "height": 1350}, None),
    ("instagram_image", {"width": 1080, "height": 1351}, "aspect_ratio_out_of_range"),
    ("instagram_image", {"width": 1080, "height": 566}, None),
    ("instagram_image", {"width": 1080, "height": 540}, "aspect_ratio_out_of_range"),
    ("instagram_image", {"width": 2000, "height": 2000}, "image_width_resized"),
    ("instagram_image", {"width": None, "height": None}, "probe_incomplete"),
    ("instagram_story", {"width": 1080, "height": 1920}, None),
    ("instagram_story", {"width": 1080, "height": 1080}, "aspect_ratio_not_recommended"),
    (
        "instagram_story",
        {"container": "png", "width": 1080, "height": 1920},
        "image_format_not_supported",
    ),
    ("instagram_carousel", {"width": 1080, "height": 1351}, "aspect_ratio_out_of_range"),
]


@pytest.mark.parametrize(("post_type", "changes", "expected"), IMAGE_ROWS)
def test_image_rows(post_type: PostType, changes: dict[str, Any], expected: str | None) -> None:
    issues = _check(_probe(IMAGE, changes), post_type)
    assert _codes(issues) == (set() if expected is None else {expected}), issues


# Kinds and platforms ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("probe", "post_type"),
    [
        (VIDEO, "instagram_image"),
        (IMAGE, "youtube_video"),
        (IMAGE, "tiktok_video"),
        (IMAGE, "instagram_reel"),
        (replace(VIDEO, kind="audio"), "tiktok_video"),
        (replace(VIDEO, kind="unknown"), "instagram_story"),
    ],
)
def test_wrong_media_kind_is_a_single_error(probe: ProbeResult, post_type: PostType) -> None:
    [issue] = _check(probe, post_type)
    assert (issue.severity, issue.code) == ("error", "media_kind_not_supported")


def test_post_type_must_belong_to_the_platform() -> None:
    with pytest.raises(ValueError):
        validate(VIDEO, Platform.youtube, "tiktok_video")
    with pytest.raises(ValueError):
        validate(VIDEO, "youtube", "youtube_live")  # type: ignore[arg-type]


def test_a_file_valid_for_youtube_and_tiktok_is_invalid_for_instagram() -> None:
    payload = json.loads((FIXTURES / "vp9_opus_webm.json").read_text(encoding="utf-8"))
    webm = parse_ffprobe(payload)
    assert _codes(_check(webm, "youtube_video"), "error") == set()
    assert _codes(_check(webm, "tiktok_video"), "error") == set()
    reel_errors = _codes(_check(webm, "instagram_reel"), "error")
    assert reel_errors == {
        "container_not_supported",
        "video_codec_not_supported",
        "audio_codec_not_supported",
        "duration_too_short",
    }

    large_mp4 = replace(VIDEO, size_bytes=500 * MB)
    assert _check(large_mp4, "youtube_video") == []
    assert _check(large_mp4, "tiktok_video") == []
    assert _codes(_check(large_mp4, "instagram_reel")) == {"file_too_large"}


# Carousels -------------------------------------------------------------------------------


def test_carousel_count_and_child_rules() -> None:
    assert _codes(validate_carousel([IMAGE])) == {"carousel_item_count"}
    assert _codes(validate_carousel([IMAGE] * 21)) == {"carousel_item_count"}
    assert validate_carousel([IMAGE, VIDEO]) == []

    png = replace(IMAGE, container="png")
    webm = replace(VIDEO, container="webm", moov_at_front=None)
    issues = validate_carousel([IMAGE, png, webm])
    assert {(issue.item, issue.code) for issue in issues} == {
        (1, "image_format_not_supported"),
        (2, "container_not_supported"),
    }


def test_carousel_warns_when_images_will_be_cropped() -> None:
    square = replace(IMAGE, width=1080, height=1080)
    issues = validate_carousel([IMAGE, square])
    assert [(issue.item, issue.code, issue.severity) for issue in issues] == [
        (1, "carousel_image_cropped", "warning")
    ]
