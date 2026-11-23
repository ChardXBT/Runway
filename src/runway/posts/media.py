"""Media wiring for posts: which stores exist and how a probed asset is validated.

Validation per target is the mediastore's pure `validate(probe, platform,
post_type)` (spec 02 section 5), applied to the probe stored on the asset.
"""

from __future__ import annotations

import json
from typing import Any

from runway.config import Settings
from runway.db.models import MediaAssetV2
from runway.mediastore import LocalDiskStore, MediaStore, R2Config, R2Store
from runway.mediastore import validate as validate_probe
from runway.mediastore.probe import ProbeResult
from runway.platforms.publisher import Issue
from runway.posts.types import TargetPostType, platform_of

# Post types the mediastore validates under another name: an inbox draft is a
# TikTok video; YouTube Community-post images follow posts.imageUpload rules.
_VALIDATED_AS: dict[TargetPostType, str] = {
    TargetPostType.TIKTOK_DRAFT: TargetPostType.TIKTOK_VIDEO.value,
}
YOUTUBE_POST_IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/gif", "image/webp"})
YOUTUBE_POST_IMAGE_MAX_BYTES = 16 * 1024 * 1024
YOUTUBE_POST_ASPECT = (2 / 5, 5 / 2)
_YOUTUBE_POST_SOURCE = "YouTube posts.imageUpload, read 2026-11-22"


def r2_config(settings: Settings) -> R2Config | None:
    if not settings.r2_configured:
        return None
    assert settings.r2_account_id and settings.r2_bucket
    assert settings.r2_access_key_id and settings.r2_secret_access_key
    return R2Config(
        account_id=settings.r2_account_id,
        bucket=settings.r2_bucket,
        access_key_id=settings.r2_access_key_id,
        secret_access_key=settings.r2_secret_access_key,
        endpoint=settings.r2_endpoint,
    )


def default_media_stores(settings: Settings) -> dict[str, MediaStore]:
    """`local` always; `r2` when its settings are configured (no network at construction)."""
    stores: dict[str, MediaStore] = {
        "local": LocalDiskStore.from_settings(
            settings,
            part_size=settings.media_api_chunk_bytes,
            max_object_bytes=settings.media_max_upload_bytes,
        )
    }
    config = r2_config(settings)
    if config is not None:
        stores["r2"] = R2Store(config, max_object_bytes=settings.media_max_upload_bytes)
    return stores


def stored_probe(asset: MediaAssetV2) -> ProbeResult | None:
    try:
        payload: Any = json.loads(asset.probe_json or "{}")
    except ValueError:
        return None
    if not isinstance(payload, dict) or "kind" not in payload:
        return None
    try:
        return ProbeResult.from_dict(payload)
    except TypeError:
        return None


def _youtube_post_issues(asset: MediaAssetV2, probe: ProbeResult) -> list[Issue]:
    issues: list[Issue] = []
    if asset.content_type not in YOUTUBE_POST_IMAGE_TYPES:
        issues.append(
            Issue(
                "error",
                "image_format",
                "Community-post images must be JPEG, PNG, GIF or WebP",
                source=_YOUTUBE_POST_SOURCE,
            )
        )
    if int(asset.size_bytes) > YOUTUBE_POST_IMAGE_MAX_BYTES:
        issues.append(
            Issue(
                "error",
                "file_too_large",
                "Community-post images are limited to 16 MB",
                source=_YOUTUBE_POST_SOURCE,
            )
        )
    aspect = probe.aspect_ratio
    low, high = YOUTUBE_POST_ASPECT
    if aspect is not None and not low <= aspect <= high:
        issues.append(
            Issue(
                "error",
                "aspect_ratio",
                "Community-post images must be between 2:5 and 5:2",
                source=_YOUTUBE_POST_SOURCE,
            )
        )
    return issues


def probe_media_validator(asset: MediaAssetV2, post_type: TargetPostType) -> list[Issue]:
    """Per-target validation of one ready asset from its stored probe (spec 02 section 5)."""
    probe = stored_probe(asset)
    if probe is None:
        return [
            Issue(
                "error",
                "probe_missing",
                "this media item has not been checked yet",
                fix="wait for the check to finish, or upload it again",
            )
        ]
    if post_type is TargetPostType.YOUTUBE_POST:
        return _youtube_post_issues(asset, probe)
    name = _VALIDATED_AS.get(post_type, post_type.value)
    found = validate_probe(probe, platform_of(post_type), name)  # type: ignore[arg-type]
    return [
        Issue(
            issue.severity,
            issue.code,
            issue.message,
            fix=issue.fix,
            source=issue.source,
            item=issue.item,
        )
        for issue in found
    ]
