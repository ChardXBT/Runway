"""Per-target media validation (spec 02 §5).

``validate`` is pure: it reads a :class:`ProbeResult` and the limits in
:mod:`runway.platforms.limits`, and returns issues. ``error`` blocks release for that target;
``warning`` is shown and does not block. Runway does not transcode in phase 1, so each issue
that a re-mux or re-encode would solve names the ffmpeg command.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from runway.mediastore.probe import MediaKind, ProbeResult
from runway.platforms.capabilities import Platform
from runway.platforms.limits import (
    IMAGE_LIMITS,
    INSTAGRAM_CAROUSEL,
    MB,
    POST_TYPE_PLATFORM,
    VALIDATION_HEURISTICS,
    VIDEO_LIMITS,
    YOUTUBE_SHORTS_HEURISTIC,
    ImageLimits,
    PostType,
    VideoLimits,
)

Severity = Literal["error", "warning"]

FASTSTART_FIX = "ffmpeg -i input.mp4 -c copy -movflags +faststart output.mp4"
EDIT_LIST_FIX = "ffmpeg -i input.mp4 -c copy -use_editlist 0 -movflags +faststart output.mp4"
H264_AAC_FIX = (
    "ffmpeg -i input -c:v libx264 -pix_fmt yuv420p -c:a aac -ar 48000 -ac 2 "
    "-movflags +faststart output.mp4"
)
JPEG_FIX = "ffmpeg -i input -q:v 2 output.jpg"

_POST_TYPE_LABELS: dict[PostType, str] = {
    "youtube_video": "YouTube videos",
    "youtube_short": "YouTube Shorts",
    "instagram_reel": "Instagram Reels",
    "instagram_image": "Instagram image posts",
    "instagram_carousel": "Instagram carousel items",
    "instagram_story": "Instagram Stories",
    "tiktok_video": "TikTok videos",
}
_ACCEPTED_KINDS: dict[PostType, frozenset[MediaKind]] = {
    "youtube_video": frozenset({"video"}),
    "youtube_short": frozenset({"video"}),
    "instagram_reel": frozenset({"video"}),
    "instagram_image": frozenset({"image"}),
    "instagram_carousel": frozenset({"image", "video"}),
    "instagram_story": frozenset({"image", "video"}),
    "tiktok_video": frozenset({"video"}),
}
_KIND_PHRASES: dict[MediaKind, str] = {
    "video": "a video",
    "image": "an image",
    "audio": "audio only",
    "unknown": "not a recognised image or video",
}
_NAMES = {
    "mp4": "MP4",
    "mov": "MOV",
    "webm": "WebM",
    "matroska": "Matroska",
    "h264": "H.264",
    "hevc": "HEVC (H.265)",
    "vp8": "VP8",
    "vp9": "VP9",
    "av1": "AV1",
    "aac": "AAC",
    "opus": "Opus",
    "jpeg": "JPEG",
    "png": "PNG",
    "webp": "WebP",
}


@dataclass(frozen=True)
class Issue:
    severity: Severity
    code: str
    message: str
    fix: str | None = None
    # Where the limit comes from (limits module ``source``), for the editor to show.
    source: str | None = None
    # Position of the carousel child the issue is about, from 0.
    item: int | None = None


def _name(value: str | None) -> str:
    if value is None:
        return "unknown"
    return _NAMES.get(value, value)


def _names(values: frozenset[str]) -> str:
    ordered = sorted(_name(value) for value in values)
    return ordered[0] if len(ordered) == 1 else ", ".join(ordered[:-1]) + " or " + ordered[-1]


def _megabytes(size: int) -> str:
    return f"{size / MB:,.1f} MB" if size < 10 * MB else f"{size / MB:,.0f} MB"


def _duration(ms: int) -> str:
    seconds = ms / 1000
    if seconds < 120:
        return f"{seconds:g} s"
    return f"{seconds / 60:g} min"


def _ratio(value: float) -> str:
    return f"{value:.2f}:1"


class _Collector:
    def __init__(self, label: str, source: str) -> None:
        self.label = label
        self.source = source
        self.issues: list[Issue] = []

    def error(self, code: str, message: str, fix: str | None = None) -> None:
        self.issues.append(Issue("error", code, message, fix, self.source))

    def warning(self, code: str, message: str, fix: str | None = None) -> None:
        self.issues.append(Issue("warning", code, message, fix, self.source))

    def unknown(self, what: str) -> None:
        self.warning(
            "probe_incomplete",
            f"Could not read the {what} of this file, so {self.label} rules for it were not "
            "checked. The platform may still reject it.",
        )


def _check_size(out: _Collector, size: int | None, limit: int) -> None:
    if size is None:
        out.unknown("size")
    elif size > limit:
        out.error(
            "file_too_large",
            f"The file is {_megabytes(size)}; {out.label} accept at most {_megabytes(limit)}.",
        )


def _check_aspect(
    out: _Collector,
    probe: ProbeResult,
    min_aspect: float | None,
    max_aspect: float | None,
    recommended: float | None,
) -> None:
    aspect = probe.aspect_ratio
    if aspect is None:
        if min_aspect is not None or max_aspect is not None or recommended is not None:
            out.unknown("dimensions")
        return
    if (min_aspect is not None and aspect < min_aspect) or (
        max_aspect is not None and aspect > max_aspect
    ):
        low = _ratio(min_aspect) if min_aspect is not None else "any"
        high = _ratio(max_aspect) if max_aspect is not None else "any"
        out.error(
            "aspect_ratio_out_of_range",
            f"The aspect ratio is {_ratio(aspect)} ({probe.width}x{probe.height}); "
            f"{out.label} accept {low} to {high}.",
        )
        return
    if recommended is not None:
        tolerance = VALIDATION_HEURISTICS.aspect_tolerance
        if abs(aspect - recommended) / recommended > tolerance:
            out.warning(
                "aspect_ratio_not_recommended",
                f"The aspect ratio is {_ratio(aspect)} ({probe.width}x{probe.height}); "
                f"{out.label} are shown best at 9:16 (vertical).",
            )


def _validate_video(out: _Collector, probe: ProbeResult, limits: VideoLimits) -> None:
    _check_size(out, probe.size_bytes, limits.max_size_bytes)

    if limits.min_duration_ms is not None or limits.max_duration_ms is not None:
        if probe.duration_ms is None:
            out.unknown("duration")
        elif limits.min_duration_ms is not None and probe.duration_ms < limits.min_duration_ms:
            out.error(
                "duration_too_short",
                f"The video is {_duration(probe.duration_ms)} long; {out.label} must be at "
                f"least {_duration(limits.min_duration_ms)}.",
            )
        elif limits.max_duration_ms is not None and probe.duration_ms > limits.max_duration_ms:
            out.error(
                "duration_too_long",
                f"The video is {_duration(probe.duration_ms)} long; {out.label} can be at "
                f"most {_duration(limits.max_duration_ms)}.",
            )

    container_ok = True
    if limits.containers is not None and probe.container not in limits.containers:
        container_ok = False
        out.error(
            "container_not_supported",
            f"The file is {_name(probe.container)}; {out.label} accept "
            f"{_names(limits.containers)}.",
            H264_AAC_FIX,
        )
    if container_ok and limits.require_moov_at_front:
        if probe.moov_at_front is None:
            out.unknown("MP4 layout (moov position)")
        elif not probe.moov_at_front:
            out.error(
                "moov_not_at_front",
                f"The file's index (moov atom) is at the end; {out.label} need it at the "
                "front. Re-save the file with faststart; this does not re-encode it.",
                FASTSTART_FIX,
            )
    if container_ok and limits.forbid_edit_lists and probe.has_edit_list is None:
        out.unknown("MP4 layout (edit lists)")
    elif container_ok and limits.forbid_edit_lists and probe.has_edit_list:
        out.error(
            "edit_list_present",
            f"The file has an edit list (an elst box, which most encoders write to shift a "
            f"track's start); {out.label} do not accept edit lists. Re-save the file without "
            "one; this does not re-encode it.",
            EDIT_LIST_FIX,
        )

    if limits.video_codecs is not None and probe.video_codec not in limits.video_codecs:
        out.error(
            "video_codec_not_supported",
            f"The video codec is {_name(probe.video_codec)}; {out.label} accept "
            f"{_names(limits.video_codecs)}.",
            H264_AAC_FIX,
        )

    if probe.audio_codec is not None:
        if limits.audio_codecs is not None and probe.audio_codec not in limits.audio_codecs:
            out.error(
                "audio_codec_not_supported",
                f"The audio codec is {_name(probe.audio_codec)}; {out.label} accept "
                f"{_names(limits.audio_codecs)}.",
                H264_AAC_FIX,
            )
        rate_limit = limits.max_audio_sample_rate_hz
        sample_rate = probe.audio_sample_rate
        if rate_limit is not None and sample_rate is not None and sample_rate > rate_limit:
            out.error(
                "audio_sample_rate_too_high",
                f"The audio sample rate is {sample_rate / 1000:g} kHz; "
                f"{out.label} accept at most {rate_limit / 1000:g} kHz.",
                H264_AAC_FIX,
            )
        channel_limit = limits.max_audio_channels
        channels = probe.audio_channels
        if channel_limit is not None and channels is not None and channels > channel_limit:
            out.error(
                "audio_channels_too_many",
                f"The audio has {channels} channels; {out.label} accept mono or stereo.",
                H264_AAC_FIX,
            )

    if limits.min_fps is not None or limits.max_fps is not None:
        if probe.fps is None:
            out.unknown("frame rate")
        elif (limits.min_fps is not None and probe.fps < limits.min_fps) or (
            limits.max_fps is not None and probe.fps > limits.max_fps
        ):
            out.error(
                "frame_rate_out_of_range",
                f"The frame rate is {probe.fps:g} fps; {out.label} accept "
                f"{limits.min_fps:g} to {limits.max_fps:g} fps.",
            )

    needs_dimensions = (
        limits.max_width is not None or limits.min_side is not None or limits.max_side is not None
    )
    if needs_dimensions and (probe.width is None or probe.height is None):
        out.unknown("dimensions")
    elif probe.width is not None and probe.height is not None:
        if limits.max_width is not None and probe.width > limits.max_width:
            out.error(
                "width_too_large",
                f"The video is {probe.width} px wide; {out.label} accept at most "
                f"{limits.max_width} px.",
            )
        short_side, long_side = sorted((probe.width, probe.height))
        if (limits.min_side is not None and short_side < limits.min_side) or (
            limits.max_side is not None and long_side > limits.max_side
        ):
            out.error(
                "dimensions_out_of_range",
                f"The video is {probe.width}x{probe.height}; each side of {out.label} must be "
                f"{limits.min_side} to {limits.max_side} px.",
            )

    _check_aspect(out, probe, limits.min_aspect, limits.max_aspect, limits.recommended_aspect)

    max_bitrate = limits.max_bitrate_bps
    if max_bitrate is not None and probe.bitrate is not None and probe.bitrate > max_bitrate:
        out.warning(
            "bitrate_above_recommended",
            f"The bitrate is {probe.bitrate / 1_000_000:.1f} Mbps; {out.label} are "
            f"documented up to {max_bitrate / 1_000_000:g} Mbps.",
        )


def _validate_image(out: _Collector, probe: ProbeResult, limits: ImageLimits) -> None:
    if probe.container not in limits.formats:
        out.error(
            "image_format_not_supported",
            f"The image is {_name(probe.container)}; {out.label} accept "
            f"{_names(limits.formats)} only.",
            JPEG_FIX,
        )
    _check_size(out, probe.size_bytes, limits.max_size_bytes)
    if probe.width is None or probe.height is None:
        out.unknown("dimensions")
    else:
        if (limits.min_width is not None and probe.width < limits.min_width) or (
            limits.max_width is not None and probe.width > limits.max_width
        ):
            out.warning(
                "image_width_resized",
                f"The image is {probe.width} px wide; {out.label} are resized to "
                f"{limits.min_width}-{limits.max_width} px wide.",
            )
        if limits.max_side is not None and max(probe.width, probe.height) > limits.max_side:
            out.error(
                "dimensions_out_of_range",
                f"The image is {probe.width}x{probe.height}; {out.label} accept at most "
                f"{limits.max_side} px on the longer side.",
            )
    _check_aspect(out, probe, limits.min_aspect, limits.max_aspect, limits.recommended_aspect)


def _validate_short(out: _Collector, probe: ProbeResult) -> None:
    heuristic = YOUTUBE_SHORTS_HEURISTIC
    aspect = probe.aspect_ratio
    if aspect is not None and aspect > heuristic.max_aspect:
        out.warning(
            "short_not_vertical",
            f"The video is {probe.width}x{probe.height} (landscape). YouTube classifies only "
            "vertical or square videos as Shorts; this one will probably be a regular video. "
            "(Heuristic: the API has no Shorts flag.)",
        )
    if probe.duration_ms is not None and probe.duration_ms > heuristic.max_duration_ms:
        out.warning(
            "short_too_long",
            f"The video is {_duration(probe.duration_ms)} long. Runway treats videos up to "
            f"{_duration(heuristic.max_duration_ms)} as Shorts; this one will probably be a "
            "regular video. (Heuristic: the API has no Shorts flag.)",
        )


def validate(
    probe: ProbeResult,
    platform: Platform | str,
    post_type: PostType,
    *,
    creator_max_duration_s: int | None = None,
) -> list[Issue]:
    """Return the issues that stop or affect publishing ``probe`` as ``post_type``.

    ``creator_max_duration_s`` is TikTok's ``max_video_post_duration_sec`` from
    ``creator_info``, when the caller has it.
    """
    if post_type not in POST_TYPE_PLATFORM:
        raise ValueError(f"unknown post type {post_type!r}")
    if POST_TYPE_PLATFORM[post_type] != Platform(platform):
        raise ValueError(f"post type {post_type} does not belong to platform {platform}")

    label = _POST_TYPE_LABELS[post_type]
    accepted = _ACCEPTED_KINDS[post_type]
    if probe.kind not in accepted:
        wanted = " or ".join(_KIND_PHRASES[kind] for kind in sorted(accepted))
        return [
            Issue(
                "error",
                "media_kind_not_supported",
                f"This file is {_KIND_PHRASES[probe.kind]}; {label} need {wanted}.",
            )
        ]

    if probe.kind == "image":
        image_limits = IMAGE_LIMITS[post_type]
        out = _Collector(label, image_limits.source)
        _validate_image(out, probe, image_limits)
        return out.issues

    video_limits = VIDEO_LIMITS[post_type]
    out = _Collector(label, video_limits.source)
    _validate_video(out, probe, video_limits)
    if post_type == "youtube_short":
        out.source = YOUTUBE_SHORTS_HEURISTIC.source
        _validate_short(out, probe)
    if (
        post_type == "tiktok_video"
        and creator_max_duration_s is not None
        and probe.duration_ms is not None
        and probe.duration_ms > creator_max_duration_s * 1000
    ):
        out.source = "TikTok creator_info max_video_post_duration_sec"
        out.error(
            "duration_exceeds_creator_limit",
            f"The video is {_duration(probe.duration_ms)} long; this TikTok account can post "
            f"at most {_duration(creator_max_duration_s * 1000)}.",
        )
    return out.issues


def validate_carousel(children: Sequence[ProbeResult]) -> list[Issue]:
    """Validate an Instagram carousel: the child count, then each child by its own kind."""
    issues: list[Issue] = []
    limits = INSTAGRAM_CAROUSEL
    if not limits.min_children <= len(children) <= limits.max_children:
        issues.append(
            Issue(
                "error",
                "carousel_item_count",
                f"The carousel has {len(children)} items; Instagram carousels need "
                f"{limits.min_children} to {limits.max_children}.",
                source=limits.source,
            )
        )
    first_aspect: float | None = None
    for index, child in enumerate(children):
        for issue in validate(child, Platform.instagram, "instagram_carousel"):
            issues.append(
                Issue(issue.severity, issue.code, issue.message, issue.fix, issue.source, index)
            )
        aspect = child.aspect_ratio
        if child.kind != "image" or aspect is None:
            continue
        if first_aspect is None:
            first_aspect = aspect
        elif abs(aspect - first_aspect) / first_aspect > VALIDATION_HEURISTICS.aspect_tolerance:
            issues.append(
                Issue(
                    "warning",
                    "carousel_image_cropped",
                    f"Item {index + 1} is {_ratio(aspect)}; Instagram crops carousel images to "
                    f"the first image's {_ratio(first_aspect)}.",
                    source=limits.source,
                    item=index,
                )
            )
    return issues
