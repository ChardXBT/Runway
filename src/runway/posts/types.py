"""Vocabulary of the publishing engine (spec 03): post types, attempts, derived post status."""

from __future__ import annotations

from enum import StrEnum

from runway.domain.enums import TargetState
from runway.platforms.capabilities import Platform


class TargetPostType(StrEnum):
    """What a target publishes. Spec 03 section 1, plus `youtube_post` (spec 08, 2026-11-22)."""

    YOUTUBE_VIDEO = "youtube_video"
    YOUTUBE_SHORT = "youtube_short"
    # Community post through the YouTube `posts` API (pilot; gated by
    # `youtube_posts_api_access`). The browser Lineup publisher is unrelated.
    YOUTUBE_POST = "youtube_post"
    INSTAGRAM_REEL = "instagram_reel"
    INSTAGRAM_IMAGE = "instagram_image"
    INSTAGRAM_CAROUSEL = "instagram_carousel"
    INSTAGRAM_STORY = "instagram_story"
    TIKTOK_VIDEO = "tiktok_video"
    # Inbox draft: the creator finishes and posts it in the TikTok app.
    TIKTOK_DRAFT = "tiktok_draft"


POST_TYPE_PLATFORM: dict[TargetPostType, Platform] = {
    TargetPostType.YOUTUBE_VIDEO: Platform.youtube,
    TargetPostType.YOUTUBE_SHORT: Platform.youtube,
    TargetPostType.YOUTUBE_POST: Platform.youtube,
    TargetPostType.INSTAGRAM_REEL: Platform.instagram,
    TargetPostType.INSTAGRAM_IMAGE: Platform.instagram,
    TargetPostType.INSTAGRAM_CAROUSEL: Platform.instagram,
    TargetPostType.INSTAGRAM_STORY: Platform.instagram,
    TargetPostType.TIKTOK_VIDEO: Platform.tiktok,
    TargetPostType.TIKTOK_DRAFT: Platform.tiktok,
}

# Post type given to targets that were created before post types existed
# (0017 backfill and the unversioned /api/drafts routes).
DEFAULT_POST_TYPE: dict[Platform, TargetPostType] = {
    Platform.youtube: TargetPostType.YOUTUBE_VIDEO,
    Platform.instagram: TargetPostType.INSTAGRAM_IMAGE,
    Platform.tiktok: TargetPostType.TIKTOK_VIDEO,
}


def platform_of(post_type: TargetPostType | str) -> Platform:
    return POST_TYPE_PLATFORM[TargetPostType(post_type)]


class PostSource(StrEnum):
    HUMAN = "human"
    AGENT = "agent"
    PROPOSAL = "proposal"


class ReleaseState(StrEnum):
    NONE = "none"
    RELEASED = "released"


class AttemptKind(StrEnum):
    """What a `target_attempts` row records."""

    SEND = "send"
    # Work done before `planned_at` that cannot publish (an Instagram container).
    PREPARE = "prepare"
    # A terminal answer from a processing poll.
    POLL = "poll"
    VERIFY = "verify"
    RECONCILE = "reconcile"


class AttemptOutcome(StrEnum):
    PENDING = "pending"
    PREPARED = "prepared"
    PROCESSING = "processing"
    PUBLISHED = "published"
    VERIFIED = "verified"
    NEEDS_USER = "needs_user"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_FINAL = "failed_final"
    UNCERTAIN = "uncertain"
    # The worker stopped before the point of no return; nothing was sent.
    ABANDONED = "abandoned"
    # The send was refused by the engine before any platform call
    # (payload changed, paused, missed window, rate guard).
    NOT_SENT = "not_sent"
    MISMATCH = "mismatch"
    ABSENT = "absent"


class PostStatus(StrEnum):
    """A post's status, derived from its targets and never stored (spec 03 section 2)."""

    DRAFT = "draft"
    READY = "ready"
    SCHEDULED = "scheduled"
    PUBLISHING = "publishing"
    NEEDS_ATTENTION = "needs_attention"
    PARTIALLY_PUBLISHED = "partially_published"
    PUBLISHED = "published"
    CANCELLED = "cancelled"


def target_state_name(state: TargetState | str) -> str:
    """The spec 03 name of a stored target state (`pending` is shown as `draft`)."""
    return TargetState(state).name.lower()
