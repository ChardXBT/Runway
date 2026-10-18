from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel


class Platform(StrEnum):
    youtube = "youtube"
    instagram = "instagram"
    tiktok = "tiktok"


class Capability(StrEnum):
    account_connection = "account_connection"
    account_analytics = "account_analytics"
    content_inventory = "content_inventory"
    content_analytics = "content_analytics"
    comments_read = "comments_read"
    comments_reply = "comments_reply"
    publishing = "publishing"
    scheduling = "scheduling"
    messaging = "messaging"


CapabilityState = Literal[
    "implemented",
    "supported_not_implemented",
    "approval_required",
    "unsupported",
    "unverified",
]


class CapabilityEntry(BaseModel):
    capability: Capability
    state: CapabilityState
    detail: str
    requires_scopes: list[str] = []
    docs_url: str | None = None


YOUTUBE_READONLY_SCOPE = "https://www.googleapis.com/auth/youtube.readonly"
YOUTUBE_ANALYTICS_SCOPE = "https://www.googleapis.com/auth/yt-analytics.readonly"
YOUTUBE_ENGAGE_SCOPE = "https://www.googleapis.com/auth/youtube.force-ssl"

INSTAGRAM_SCOPES = (
    "instagram_business_basic",
    "instagram_business_manage_insights",
    "instagram_business_manage_comments",
)

TIKTOK_SCOPES = (
    "user.info.basic",
    "user.info.profile",
    "user.info.stats",
    "video.list",
)

DEFAULT_PLATFORM_SCOPES: dict[Platform, list[str]] = {
    Platform.youtube: [YOUTUBE_READONLY_SCOPE, YOUTUBE_ANALYTICS_SCOPE],
    Platform.instagram: list(INSTAGRAM_SCOPES),
    Platform.tiktok: list(TIKTOK_SCOPES),
}


_GOOGLE_SCOPE_DOCS = "https://developers.google.com/youtube/v3/guides/auth/server-side-web-apps"
_INSTAGRAM_DOCS = "https://developers.facebook.com/docs/instagram-platform"
_TIKTOK_DOCS = "https://developers.tiktok.com/doc/oauth-user-access-token-management"


def youtube_capability_matrix(
    *, engage_scopes: bool, replies_enabled: bool
) -> list[CapabilityEntry]:
    connection_detail = "Google OAuth 2.0 (scopes youtube.readonly, yt-analytics.readonly" + (
        "; youtube.force-ssl for comment replies)" if engage_scopes else ")"
    )
    return [
        CapabilityEntry(
            capability=Capability.account_connection,
            state="implemented",
            detail=connection_detail,
            requires_scopes=[YOUTUBE_READONLY_SCOPE, YOUTUBE_ANALYTICS_SCOPE],
            docs_url=_GOOGLE_SCOPE_DOCS,
        ),
        CapabilityEntry(
            capability=Capability.account_analytics,
            state="implemented",
            detail="channels.list statistics plus YouTube Analytics reports.query (day dimension)",
            requires_scopes=[YOUTUBE_ANALYTICS_SCOPE],
        ),
        CapabilityEntry(
            capability=Capability.content_inventory,
            state="implemented",
            detail="playlistItems.list on the channel uploads playlist (1 quota unit per page)",
            requires_scopes=[YOUTUBE_READONLY_SCOPE],
        ),
        CapabilityEntry(
            capability=Capability.content_analytics,
            state="implemented",
            detail="videos.list statistics fetched in batches of 50",
            requires_scopes=[YOUTUBE_READONLY_SCOPE],
        ),
        CapabilityEntry(
            capability=Capability.comments_read,
            state="implemented",
            detail="commentThreads.list per video",
            requires_scopes=[YOUTUBE_READONLY_SCOPE],
        ),
        CapabilityEntry(
            capability=Capability.comments_reply,
            state="implemented",
            detail=(
                "comments.insert (50 quota units) requires youtube.force-ssl and "
                "RUNWAY_INBOX_REPLIES_ENABLED"
                if replies_enabled and engage_scopes
                else "comments.insert requires youtube.force-ssl scope and "
                "RUNWAY_INBOX_REPLIES_ENABLED=true"
            ),
            requires_scopes=[YOUTUBE_ENGAGE_SCOPE],
        ),
        CapabilityEntry(
            capability=Capability.publishing,
            state="supported_not_implemented",
            detail=(
                "videos.insert exists (youtube.upload scope, 100 inserts/day bucket) but is "
                "not implemented; the assisted browser path remains the publishing workflow"
            ),
        ),
        CapabilityEntry(
            capability=Capability.scheduling,
            state="supported_not_implemented",
            detail="videos.insert status.publishAt is not implemented; Lineup stays manual",
        ),
        CapabilityEntry(
            capability=Capability.messaging,
            state="unsupported",
            detail="YouTube has no messaging API",
        ),
    ]


def instagram_capability_matrix() -> list[CapabilityEntry]:
    return [
        CapabilityEntry(
            capability=Capability.account_connection,
            state="implemented",
            detail=(
                "Business Login for Instagram OAuth; also supports importing a "
                "long-lived token for Standard Access own-account setups"
            ),
            requires_scopes=[INSTAGRAM_SCOPES[0]],
            docs_url=_INSTAGRAM_DOCS,
        ),
        CapabilityEntry(
            capability=Capability.account_analytics,
            state="implemented",
            detail="GET /me plus /me/insights requires instagram_business_manage_insights",
            requires_scopes=[INSTAGRAM_SCOPES[0], INSTAGRAM_SCOPES[1]],
        ),
        CapabilityEntry(
            capability=Capability.content_inventory,
            state="implemented",
            detail="GET /me/media with cursor paging",
            requires_scopes=[INSTAGRAM_SCOPES[0]],
        ),
        CapabilityEntry(
            capability=Capability.content_analytics,
            state="implemented",
            detail="GET /{media}/insights; metric set depends on media_product_type",
            requires_scopes=[INSTAGRAM_SCOPES[0], INSTAGRAM_SCOPES[1]],
        ),
        CapabilityEntry(
            capability=Capability.comments_read,
            state="implemented",
            detail="GET /{media}/comments including replies",
            requires_scopes=[INSTAGRAM_SCOPES[0], INSTAGRAM_SCOPES[2]],
        ),
        CapabilityEntry(
            capability=Capability.comments_reply,
            state="implemented",
            detail="POST /{comment}/replies requires RUNWAY_INBOX_REPLIES_ENABLED=true",
            requires_scopes=[INSTAGRAM_SCOPES[0], INSTAGRAM_SCOPES[2]],
        ),
        CapabilityEntry(
            capability=Capability.publishing,
            state="approval_required",
            detail=(
                "Content publishing requires media hosted on a public URL; Runway is "
                "loopback-only so direct publishing needs a public media host. "
                "The manual mark-published workflow is provided instead"
            ),
        ),
        CapabilityEntry(
            capability=Capability.scheduling,
            state="unsupported",
            detail="The Instagram API has no scheduled publish; scheduling must be app-side",
        ),
        CapabilityEntry(
            capability=Capability.messaging,
            state="supported_not_implemented",
            detail="instagram_business_manage_messages is a separate scope, not implemented",
        ),
    ]


def tiktok_capability_matrix() -> list[CapabilityEntry]:
    return [
        CapabilityEntry(
            capability=Capability.account_connection,
            state="implemented",
            detail=(
                "Login Kit OAuth (web). TikTok requires an HTTPS redirect URI, so local "
                "use needs RUNWAY_OAUTH_REDIRECT_BASE_URL behind a tunnel"
            ),
            requires_scopes=[TIKTOK_SCOPES[0]],
            docs_url=_TIKTOK_DOCS,
        ),
        CapabilityEntry(
            capability=Capability.account_analytics,
            state="implemented",
            detail="/v2/user/info/ with user.info.stats",
            requires_scopes=[TIKTOK_SCOPES[0], TIKTOK_SCOPES[1], TIKTOK_SCOPES[2]],
        ),
        CapabilityEntry(
            capability=Capability.content_inventory,
            state="implemented",
            detail="/v2/video/list/ with cursor/has_more paging",
            requires_scopes=[TIKTOK_SCOPES[3]],
        ),
        CapabilityEntry(
            capability=Capability.content_analytics,
            state="implemented",
            detail="view/like/comment/share counts come from video.list fields",
            requires_scopes=[TIKTOK_SCOPES[3]],
        ),
        CapabilityEntry(
            capability=Capability.comments_read,
            state="supported_not_implemented",
            detail=(
                "Only available through the TikTok for Business Account API "
                "(/business/comment/list/), a separate app and approval"
            ),
        ),
        CapabilityEntry(
            capability=Capability.comments_reply,
            state="supported_not_implemented",
            detail="/business/comment/reply/create/ under the same Business API",
        ),
        CapabilityEntry(
            capability=Capability.publishing,
            state="approval_required",
            detail=(
                "Content Posting API video.publish; unaudited apps may only post "
                "private-only videos"
            ),
        ),
        CapabilityEntry(
            capability=Capability.scheduling,
            state="unsupported",
            detail="TikTok has no scheduled-publish API",
        ),
        CapabilityEntry(
            capability=Capability.messaging,
            state="unsupported",
            detail="Business Messaging API is business-only",
        ),
    ]


def capability_matrix(
    platform: Platform, *, engage_scopes: bool = False, replies_enabled: bool = False
) -> list[CapabilityEntry]:
    if platform is Platform.youtube:
        return youtube_capability_matrix(
            engage_scopes=engage_scopes, replies_enabled=replies_enabled
        )
    if platform is Platform.instagram:
        return instagram_capability_matrix()
    return tiktok_capability_matrix()


def capability_ready(
    entry: CapabilityEntry,
    *,
    account_status: str | None,
    granted_scopes: list[str],
) -> bool:
    if entry.state != "implemented" or account_status != "connected":
        return False
    return set(entry.requires_scopes) <= set(granted_scopes)
