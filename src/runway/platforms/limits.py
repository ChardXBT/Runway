"""Platform media and publishing limits as dated data (spec 00 invariant 15).

Every platform fact used by media validation, storage and delivery lives here, with the page
or research section it came from, the date it was read and how sure the source is. Code
elsewhere imports these values instead of repeating numbers.

Units: platform pages say "MB" and "GB" without saying whether they mean decimal or binary
units. Maximum sizes use decimal units (the smaller reading), so a file Runway accepts is
accepted under either reading. TikTok chunk bounds use the intersection of both readings; see
``TIKTOK_CHUNKS``.

Labels follow ``docs/expansion/README.md``: ``official`` (read on the platform's own page),
``community``, ``unverified`` (working knowledge, not re-read), ``inference`` (reasoning from
the facts), plus ``decision`` for a Runway choice recorded in a spec.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, timedelta
from types import MappingProxyType
from typing import Literal

from runway.platforms.capabilities import Platform

FactLabel = Literal["official", "community", "unverified", "inference", "decision"]

PostType = Literal[
    "youtube_video",
    "youtube_short",
    "youtube_post",
    "instagram_reel",
    "instagram_image",
    "instagram_carousel",
    "instagram_story",
    "tiktok_video",
]

KB = 1_000
MB = 1_000_000
GB = 1_000_000_000
KiB = 1024
MiB = 1024 * KiB
GiB = 1024 * MiB

RESEARCH_READ_ON = date(2026, 11, 14)
# Second research pass (research/recheck-20261122.md, on branch expansion/build).
RECHECK_READ_ON = date(2026, 11, 22)
SPEC_READ_ON = date(2026, 11, 22)

_YOUTUBE = "docs/expansion/research/youtube.md"
_INSTAGRAM = "docs/expansion/research/instagram.md"
_TIKTOK = "docs/expansion/research/tiktok.md"
_HOSTING = "docs/expansion/research/hosting-and-sync.md"
_SPEC_02 = "docs/expansion/specs/02-media-pipeline.md"
_SPEC_08 = "docs/expansion/specs/08-platform-adapters.md"
_RECHECK = "docs/expansion/research/recheck-20261122.md"


@dataclass(frozen=True)
class VideoLimits:
    """Rules for one video post type. ``None`` means the platform sets no such rule."""

    post_type: PostType
    platform: Platform
    max_size_bytes: int
    min_duration_ms: int | None
    max_duration_ms: int | None
    # Normalized container names from the probe (``mp4``, ``mov``, ``webm``); None = any video.
    containers: frozenset[str] | None
    require_moov_at_front: bool
    forbid_edit_lists: bool
    video_codecs: frozenset[str] | None
    audio_codecs: frozenset[str] | None
    max_audio_sample_rate_hz: int | None
    max_audio_channels: int | None
    min_fps: float | None
    max_fps: float | None
    max_width: int | None
    min_side: int | None
    max_side: int | None
    # Hard aspect bounds as width / height.
    min_aspect: float | None
    max_aspect: float | None
    # Recommended aspect (width / height); outside it is a warning only.
    recommended_aspect: float | None
    # Documented bitrate ceiling; exceeding it is a warning only.
    max_bitrate_bps: int | None
    source: str
    read_on: date
    label: FactLabel = "official"


@dataclass(frozen=True)
class ImageLimits:
    post_type: PostType | Literal["tiktok_photo"]
    platform: Platform
    # Normalized image container names from the probe (``jpeg``, ``png``, ``webp``).
    formats: frozenset[str]
    max_size_bytes: int
    # Width outside these bounds is resized by the platform: a warning only.
    min_width: int | None
    max_width: int | None
    max_side: int | None
    min_aspect: float | None
    max_aspect: float | None
    recommended_aspect: float | None
    source: str
    read_on: date
    label: FactLabel = "official"


@dataclass(frozen=True)
class CarouselLimits:
    min_children: int
    max_children: int
    source: str
    read_on: date
    label: FactLabel = "official"


@dataclass(frozen=True)
class ShortsHeuristic:
    """YouTube has no Shorts flag; a vertical or square video this short is treated as one."""

    max_duration_ms: int
    max_aspect: float
    source: str
    read_on: date
    label: FactLabel = "unverified"


@dataclass(frozen=True)
class TextLimit:
    field: str
    max_length: int
    unit: Literal["characters", "utf16_code_units"]
    max_hashtags: int | None
    max_mentions: int | None
    source: str
    read_on: date
    label: FactLabel = "official"


@dataclass(frozen=True)
class CountLimit:
    name: str
    max_count: int
    source: str
    read_on: date
    label: FactLabel = "official"


@dataclass(frozen=True)
class YouTubeUploadRules:
    max_size_bytes: int
    chunk_multiple_bytes: int
    default_chunk_bytes: int
    retryable_statuses: frozenset[int]
    thumbnail_max_bytes: int
    source: str
    read_on: date
    label: FactLabel = "official"


@dataclass(frozen=True)
class TikTokChunkRules:
    min_chunk_bytes: int
    max_chunk_bytes: int
    # Largest file sent as a single chunk; larger files are split.
    max_single_chunk_bytes: int
    max_final_chunk_bytes: int
    max_chunk_count: int
    default_chunk_bytes: int
    upload_url_ttl: timedelta
    source: str
    read_on: date
    label: FactLabel = "official"


@dataclass(frozen=True)
class InstagramContainerPolling:
    poll_interval: timedelta
    documented_window: timedelta
    extended_interval: timedelta
    signed_url_ttl: timedelta
    container_ttl: timedelta
    source: str
    read_on: date
    label: FactLabel = "official"


@dataclass(frozen=True)
class RateLimit:
    platform: Platform
    name: str
    max_count: int
    window: timedelta
    scope: str
    note: str
    source: str
    read_on: date
    label: FactLabel = "official"


@dataclass(frozen=True)
class ObjectStoreRules:
    presigned_url_max_ttl: timedelta
    min_part_bytes: int
    max_part_bytes: int
    max_parts: int
    default_part_bytes: int
    source: str
    read_on: date
    label: FactLabel = "official"


@dataclass(frozen=True)
class DetectionRule:
    """How Runway turns a platform rule into a check on the file, when the platform is vague."""

    name: str
    rule: str
    source: str
    read_on: date
    label: FactLabel = "decision"


@dataclass(frozen=True)
class ValidationHeuristics:
    """Runway's tolerance when comparing an aspect ratio with a recommended one."""

    aspect_tolerance: float
    source: str
    read_on: date
    label: FactLabel = "decision"


_INSTAGRAM_VIDEO_CODECS = frozenset({"h264", "hevc"})
YOUTUBE_CONTAINERS = frozenset({"mp4", "mov", "webm"})

YOUTUBE_VIDEO = VideoLimits(
    post_type="youtube_video",
    platform=Platform.youtube,
    max_size_bytes=256 * GB,
    min_duration_ms=None,
    max_duration_ms=None,  # Channel-dependent: spec 02 §5 says do not block.
    # Spec 02 §5 says "any video/*"; Runway narrows it to containers YouTube lists as
    # supported and that the probe reads with a whitelisted demuxer (probe.ALLOWED_DEMUXERS).
    containers=YOUTUBE_CONTAINERS,
    require_moov_at_front=False,
    forbid_edit_lists=False,
    video_codecs=None,
    audio_codecs=None,
    max_audio_sample_rate_hz=None,
    max_audio_channels=None,
    min_fps=None,
    max_fps=None,
    max_width=None,
    min_side=None,
    max_side=None,
    min_aspect=None,
    max_aspect=None,
    recommended_aspect=None,
    max_bitrate_bps=None,
    source=(
        f"{_YOUTUBE} §3; https://developers.google.com/youtube/v3/docs/videos/insert; "
        "containers: [Unverified] YouTube Help 'Supported YouTube file formats' (MOV, MP4, "
        "WebM among others), working knowledge not re-read, narrowed by Runway"
    ),
    read_on=RESEARCH_READ_ON,
)

YOUTUBE_SHORT = VideoLimits(
    post_type="youtube_short",
    platform=Platform.youtube,
    max_size_bytes=YOUTUBE_VIDEO.max_size_bytes,
    min_duration_ms=None,
    max_duration_ms=None,  # The Shorts length is a warning; see YOUTUBE_SHORTS_HEURISTIC.
    containers=YOUTUBE_CONTAINERS,
    require_moov_at_front=False,
    forbid_edit_lists=False,
    video_codecs=None,
    audio_codecs=None,
    max_audio_sample_rate_hz=None,
    max_audio_channels=None,
    min_fps=None,
    max_fps=None,
    max_width=None,
    min_side=None,
    max_side=None,
    min_aspect=None,
    max_aspect=None,
    recommended_aspect=None,
    max_bitrate_bps=None,
    source=f"{_YOUTUBE} §3; {_SPEC_08} §2; containers as for YOUTUBE_VIDEO",
    read_on=RESEARCH_READ_ON,
)

YOUTUBE_SHORTS_HEURISTIC = ShortsHeuristic(
    max_duration_ms=180_000,
    max_aspect=1.0,  # Vertical or square.
    source=f"{_YOUTUBE} §3 [Unverified]: the existing Runway '≤ 180 s' label is a heuristic",
    read_on=RESEARCH_READ_ON,
)

INSTAGRAM_REEL = VideoLimits(
    post_type="instagram_reel",
    platform=Platform.instagram,
    max_size_bytes=300 * MB,
    min_duration_ms=3_000,
    max_duration_ms=15 * 60_000,
    containers=frozenset({"mp4", "mov"}),
    require_moov_at_front=True,
    forbid_edit_lists=True,
    video_codecs=_INSTAGRAM_VIDEO_CODECS,
    audio_codecs=frozenset({"aac"}),
    max_audio_sample_rate_hz=48_000,
    max_audio_channels=2,
    min_fps=23.0,
    max_fps=60.0,
    max_width=1920,
    min_side=None,
    max_side=None,
    min_aspect=0.01,
    max_aspect=10.0,
    recommended_aspect=9 / 16,
    max_bitrate_bps=25_000_000,
    source=(
        f"{_INSTAGRAM} §4; https://developers.facebook.com/documentation/instagram-platform/"
        "instagram-graph-api/reference/ig-user/media"
    ),
    read_on=RESEARCH_READ_ON,
)

INSTAGRAM_STORY_VIDEO = VideoLimits(
    post_type="instagram_story",
    platform=Platform.instagram,
    max_size_bytes=100 * MB,
    min_duration_ms=3_000,
    max_duration_ms=60_000,
    containers=INSTAGRAM_REEL.containers,
    require_moov_at_front=True,
    forbid_edit_lists=True,
    video_codecs=_INSTAGRAM_VIDEO_CODECS,
    audio_codecs=INSTAGRAM_REEL.audio_codecs,
    max_audio_sample_rate_hz=48_000,
    max_audio_channels=2,
    min_fps=23.0,
    max_fps=60.0,
    max_width=1920,
    min_side=None,
    max_side=None,
    min_aspect=0.1,
    max_aspect=10.0,
    recommended_aspect=9 / 16,
    max_bitrate_bps=25_000_000,
    source=INSTAGRAM_REEL.source,
    read_on=RESEARCH_READ_ON,
)

# Meta documents no separate video rules for carousel children; Runway applies the Reel rules.
INSTAGRAM_CAROUSEL_VIDEO = replace(
    INSTAGRAM_REEL,
    post_type="instagram_carousel",
    source=f"{_INSTAGRAM} §2 and §4; Reel rules applied to carousel video children",
    label="inference",
)

TIKTOK_VIDEO = VideoLimits(
    post_type="tiktok_video",
    platform=Platform.tiktok,
    max_size_bytes=4 * GB,
    min_duration_ms=None,
    # The creator's max_video_post_duration_sec from creator_info can lower this further.
    max_duration_ms=10 * 60_000,
    containers=frozenset({"mp4", "webm", "mov"}),
    require_moov_at_front=False,
    forbid_edit_lists=False,
    video_codecs=frozenset({"h264", "hevc", "vp8", "vp9"}),
    audio_codecs=None,
    max_audio_sample_rate_hz=None,
    max_audio_channels=None,
    min_fps=23.0,
    max_fps=60.0,
    max_width=None,
    min_side=360,
    max_side=4096,
    min_aspect=None,
    max_aspect=None,
    recommended_aspect=None,
    max_bitrate_bps=None,
    source=(
        f"{_TIKTOK} §5; https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide"
    ),
    read_on=RESEARCH_READ_ON,
)

INSTAGRAM_IMAGE = ImageLimits(
    post_type="instagram_image",
    platform=Platform.instagram,
    formats=frozenset({"jpeg"}),
    max_size_bytes=8 * MB,
    min_width=320,
    max_width=1440,
    max_side=None,
    min_aspect=4 / 5,
    max_aspect=1.91,
    recommended_aspect=None,
    source=f"{_INSTAGRAM} §2 and §4; {_SPEC_02} §5",
    read_on=RESEARCH_READ_ON,
)

INSTAGRAM_CAROUSEL_IMAGE = replace(
    INSTAGRAM_IMAGE,
    post_type="instagram_carousel",
    source=f"{_INSTAGRAM} §2 and §4; image rules applied to carousel image children",
)

# Meta documents Story images only as "image or video"; Runway applies the image format and
# size rules and recommends 9:16 instead of the feed aspect range.
INSTAGRAM_STORY_IMAGE = ImageLimits(
    post_type="instagram_story",
    platform=Platform.instagram,
    formats=frozenset({"jpeg"}),
    max_size_bytes=8 * MB,
    min_width=320,
    max_width=1440,
    max_side=None,
    min_aspect=None,
    max_aspect=None,
    recommended_aspect=9 / 16,
    source=f"{_INSTAGRAM} §2 and §4; feed image format and size applied to Stories",
    read_on=RESEARCH_READ_ON,
    label="inference",
)

# TikTok photo posts are deferred (spec 02 §2); recorded so the fact has one home.
TIKTOK_PHOTO = ImageLimits(
    post_type="tiktok_photo",
    platform=Platform.tiktok,
    formats=frozenset({"jpeg", "webp"}),
    max_size_bytes=20 * MB,
    min_width=None,
    max_width=None,
    max_side=1080,  # "max 1080p" read as the longer side.
    min_aspect=None,
    max_aspect=None,
    recommended_aspect=None,
    source=f"{_TIKTOK} §5",
    read_on=RESEARCH_READ_ON,
)

# YouTube Community-post images (`posts.imageUpload`, pilot since 2026-11-16).
# Not in IMAGE_LIMITS: `gif` has no whitelisted demuxer, and posts/media.py
# applies these rules itself for the youtube_post post type.
YOUTUBE_POST_IMAGE = ImageLimits(
    post_type="youtube_post",
    platform=Platform.youtube,
    formats=frozenset({"jpeg", "png", "gif", "webp"}),
    max_size_bytes=16 * MiB,
    min_width=None,
    max_width=None,
    max_side=None,
    min_aspect=2 / 5,
    max_aspect=5 / 2,
    recommended_aspect=None,
    source=f"{_RECHECK} §1; {_SPEC_08} §2",
    read_on=RECHECK_READ_ON,
)

# The MIME forms of YOUTUBE_POST_IMAGE.formats, for content-type checks.
YOUTUBE_POST_IMAGE_TYPES = frozenset(f"image/{fmt}" for fmt in YOUTUBE_POST_IMAGE.formats)

INSTAGRAM_CAROUSEL = CarouselLimits(
    min_children=2,
    max_children=20,
    source=f"{_INSTAGRAM} §2; {_SPEC_08} §3",
    read_on=RESEARCH_READ_ON,
)

VIDEO_LIMITS: Mapping[PostType, VideoLimits] = MappingProxyType(
    {
        "youtube_video": YOUTUBE_VIDEO,
        "youtube_short": YOUTUBE_SHORT,
        "instagram_reel": INSTAGRAM_REEL,
        "instagram_carousel": INSTAGRAM_CAROUSEL_VIDEO,
        "instagram_story": INSTAGRAM_STORY_VIDEO,
        "tiktok_video": TIKTOK_VIDEO,
    }
)

IMAGE_LIMITS: Mapping[PostType, ImageLimits] = MappingProxyType(
    {
        "instagram_image": INSTAGRAM_IMAGE,
        "instagram_carousel": INSTAGRAM_CAROUSEL_IMAGE,
        "instagram_story": INSTAGRAM_STORY_IMAGE,
    }
)

POST_TYPE_PLATFORM: Mapping[PostType, Platform] = MappingProxyType(
    {
        "youtube_video": Platform.youtube,
        "youtube_short": Platform.youtube,
        "instagram_reel": Platform.instagram,
        "instagram_image": Platform.instagram,
        "instagram_carousel": Platform.instagram,
        "instagram_story": Platform.instagram,
        "tiktok_video": Platform.tiktok,
    }
)

INSTAGRAM_EDIT_LIST_RULE = DetectionRule(
    name="instagram.no_edit_lists",
    rule=(
        "Any elst box with at least one entry, in any track, counts as an edit list. ffmpeg's "
        "default MP4 output has one (a start shift); the fix is a re-mux with -use_editlist 0."
    ),
    source=(
        f"{_INSTAGRAM} §4 ('moov atom at front, no edit lists'). No source was found that "
        "exempts single-entry start-shift edit lists, so none is exempted."
    ),
    read_on=SPEC_READ_ON,
)

VALIDATION_HEURISTICS = ValidationHeuristics(
    aspect_tolerance=0.01,  # Relative: 1080x1916 still counts as 9:16.
    source=f"Runway choice for the 'warn outside 9:16' rows of {_SPEC_02} §5",
    read_on=SPEC_READ_ON,
)

# Text fields ------------------------------------------------------------------------------

YOUTUBE_TITLE = TextLimit(
    field="youtube.title",
    max_length=100,
    unit="characters",
    max_hashtags=None,
    max_mentions=None,
    source=f"{_SPEC_08} §2 [working knowledge; the API returns invalidTitle otherwise]",
    read_on=SPEC_READ_ON,
    label="unverified",
)

YOUTUBE_DESCRIPTION = TextLimit(
    field="youtube.description",
    max_length=5_000,
    unit="characters",
    max_hashtags=None,
    max_mentions=None,
    source=f"{_SPEC_08} §2 [working knowledge; the API returns invalidDescription otherwise]",
    read_on=SPEC_READ_ON,
    label="unverified",
)

INSTAGRAM_CAPTION = TextLimit(
    field="instagram.caption",
    max_length=2_200,
    unit="characters",
    max_hashtags=30,
    max_mentions=20,
    source=f"{_INSTAGRAM} §2",
    read_on=RESEARCH_READ_ON,
)

INSTAGRAM_ALT_TEXT = TextLimit(
    field="instagram.alt_text",
    max_length=1_000,
    unit="characters",
    max_hashtags=None,
    max_mentions=None,
    source=f"{_INSTAGRAM} §2 (added 2025-03-24)",
    read_on=RESEARCH_READ_ON,
)

INSTAGRAM_COLLABORATORS = CountLimit(
    name="instagram.collaborators",
    max_count=3,
    source=f"{_INSTAGRAM} §2 (not on Stories)",
    read_on=RESEARCH_READ_ON,
)

TIKTOK_TITLE = TextLimit(
    field="tiktok.title",
    max_length=2_200,
    unit="utf16_code_units",
    max_hashtags=None,
    max_mentions=None,
    source=f"{_TIKTOK} §4; https://developers.tiktok.com/doc/content-posting-api-reference-direct-post",
    read_on=RESEARCH_READ_ON,
)

# Delivery ---------------------------------------------------------------------------------

YOUTUBE_UPLOAD = YouTubeUploadRules(
    max_size_bytes=YOUTUBE_VIDEO.max_size_bytes,
    chunk_multiple_bytes=256 * KiB,
    default_chunk_bytes=8 * MiB,  # Spec 02 §6: 8 MB, a multiple of 256 KB.
    retryable_statuses=frozenset({500, 502, 503, 504}),
    thumbnail_max_bytes=50 * MB,  # Since 2026-09-14.
    source=(
        f"{_YOUTUBE} §3; "
        "https://developers.google.com/youtube/v3/guides/using_resumable_upload_protocol"
    ),
    read_on=RESEARCH_READ_ON,
)

# TikTok's media transfer guide (re-read 2026-11-22, research/recheck-20261122.md §3): each
# chunk 5-64 MB except the last, which may be up to 128 MB; total_chunk_count =
# floor(video_size / chunk_size); files under 5 MB go whole. Its examples use decimal sizes
# (chunk_size 10000000). The bounds below hold under both the decimal and the binary reading:
# the minimum is 5 MiB (the larger reading) and the maximums are decimal (the smaller one).
# Runway's safer reading for a one-request upload: the single chunk is also the first chunk,
# so it must respect max_chunk_bytes; a file above 64 MB is always split into 2+ chunks.
TIKTOK_CHUNKS = TikTokChunkRules(
    min_chunk_bytes=5 * MiB,
    max_chunk_bytes=64 * MB,
    max_single_chunk_bytes=64 * MB,
    max_final_chunk_bytes=128 * MB,
    max_chunk_count=1_000,
    default_chunk_bytes=10 * MB,  # Spec 02 §6 and spec 08 §4.
    upload_url_ttl=timedelta(hours=1),
    source=(
        f"{_TIKTOK} §4 and §5; {_RECHECK} §3; "
        "https://developers.tiktok.com/doc/content-posting-api-media-transfer-guide"
    ),
    read_on=RECHECK_READ_ON,
)

INSTAGRAM_POLLING = InstagramContainerPolling(
    poll_interval=timedelta(minutes=1),
    documented_window=timedelta(minutes=5),
    # Spec 02 §6 [Inference]: after the documented window, poll every 5 minutes up to the
    # signed URL's lifetime for large Reels.
    extended_interval=timedelta(minutes=5),
    signed_url_ttl=timedelta(hours=2),
    container_ttl=timedelta(hours=24),
    source=f"{_INSTAGRAM} §2; {_SPEC_02} §2 and §6",
    read_on=RESEARCH_READ_ON,
)

# Daily and rate limits ---------------------------------------------------------------------

YOUTUBE_DAILY_UPLOADS = RateLimit(
    platform=Platform.youtube,
    name="videos.insert",
    max_count=100,
    window=timedelta(days=1),
    scope="per Google Cloud project, shared by every workspace",
    note="Own quota bucket, cost 1 per call; resets at Pacific midnight.",
    source=f"{_YOUTUBE} §1; https://developers.google.com/youtube/v3/determine_quota_cost",
    read_on=RESEARCH_READ_ON,
)

# `posts.insert` costs 1 unit against a separate "Post Uploads" bucket; its
# ceiling is not public, so Runway's own guard keeps the count low.
YOUTUBE_POST_QUOTA = RateLimit(
    platform=Platform.youtube,
    name="posts.insert",
    max_count=5,
    window=timedelta(days=1),
    scope="per connected channel (Runway guard); the pilot bucket is separate from videos.insert",
    note="1 'Post Uploads' unit per insert; five posts per channel per Pacific day stays under it",
    source=f"{_RECHECK} §1; {_SPEC_08} §2",
    read_on=RECHECK_READ_ON,
    label="decision",
)

INSTAGRAM_DAILY_POSTS = RateLimit(
    platform=Platform.instagram,
    name="media_publish",
    max_count=50,
    window=timedelta(hours=24),
    scope="per Instagram account, rolling 24 hours; a carousel counts once",
    note=(
        "The page also says 100; assume 50 until GET /<IG_ID>/content_publishing_limit "
        "reports a higher quota."
    ),
    source=f"{_INSTAGRAM} §3",
    read_on=RESEARCH_READ_ON,
)

INSTAGRAM_DAILY_CONTAINERS = RateLimit(
    platform=Platform.instagram,
    name="media containers",
    max_count=400,
    window=timedelta(hours=24),
    scope="per Instagram account, rolling 24 hours",
    note="Containers also expire 24 hours after creation.",
    source=f"{_INSTAGRAM} §2",
    read_on=RESEARCH_READ_ON,
)

TIKTOK_DAILY_DIRECT_POSTS = RateLimit(
    platform=Platform.tiktok,
    name="direct post",
    max_count=15,
    window=timedelta(hours=24),
    scope="per creator, shared across all Direct Post clients",
    note="'About 15'; creator_info and the error spam_risk_too_many_posts are authoritative.",
    source=f"{_TIKTOK} §3",
    read_on=RESEARCH_READ_ON,
)

TIKTOK_DAILY_INBOX_SHARES = RateLimit(
    platform=Platform.tiktok,
    name="inbox upload",
    max_count=5,
    window=timedelta(hours=24),
    scope="pending shares per creator",
    note="",
    source=f"{_TIKTOK} §3",
    read_on=RESEARCH_READ_ON,
)

TIKTOK_UNAUDITED_DAILY_USERS = RateLimit(
    platform=Platform.tiktok,
    name="unaudited client posters",
    max_count=5,
    window=timedelta(hours=24),
    scope="distinct users per unaudited client",
    note="Unaudited clients can only post SELF_ONLY to private accounts.",
    source=f"{_TIKTOK} §2",
    read_on=RESEARCH_READ_ON,
)

TIKTOK_INIT_RATE = RateLimit(
    platform=Platform.tiktok,
    name="video init",
    max_count=6,
    window=timedelta(minutes=1),
    scope="per user access token",
    note="Direct Post and inbox init share this rule.",
    source=f"{_TIKTOK} §3",
    read_on=RESEARCH_READ_ON,
)

TIKTOK_STATUS_FETCH_RATE = RateLimit(
    platform=Platform.tiktok,
    name="status fetch",
    max_count=30,
    window=timedelta(minutes=1),
    scope="per user access token",
    note="",
    source=f"{_TIKTOK} §6",
    read_on=RESEARCH_READ_ON,
)

DAILY_PUBLISH_LIMITS: tuple[RateLimit, ...] = (
    YOUTUBE_DAILY_UPLOADS,
    YOUTUBE_POST_QUOTA,
    INSTAGRAM_DAILY_POSTS,
    INSTAGRAM_DAILY_CONTAINERS,
    TIKTOK_DAILY_DIRECT_POSTS,
    TIKTOK_DAILY_INBOX_SHARES,
    TIKTOK_UNAUDITED_DAILY_USERS,
)

# Object storage ------------------------------------------------------------------------------

# Presigned URL lifetime: official (research/hosting-and-sync.md §1, read 2026-11-14). Part
# rules are the S3 multipart rules R2 follows (5 MiB minimum except the last part, 5 GiB
# maximum, 10,000 parts); working knowledge from https://developers.cloudflare.com/r2/platform/
# limits/, not re-read for this module. R2 also expects every part except the last to have the
# same size, which Runway's fixed-size plans always satisfy.
R2_STORAGE = ObjectStoreRules(
    presigned_url_max_ttl=timedelta(days=7),
    min_part_bytes=5 * MiB,
    max_part_bytes=5 * GiB,
    max_parts=10_000,
    default_part_bytes=16 * MiB,
    source=(
        f"{_HOSTING} §1; https://developers.cloudflare.com/r2/api/s3/presigned-urls/; "
        "https://developers.cloudflare.com/r2/platform/limits/"
    ),
    read_on=RESEARCH_READ_ON,
    label="unverified",
)
