"""Typed target settings per post type (spec 08 sections 2-4, re-check 2026-11-22).

Two levels of checking:

- Structure (`parse_settings`): unknown fields and wrong types are refused at
  once (HTTP 422). A field that does not exist for the post type - for
  example `alt_text` on a Reel - is unknown.
- Completeness (`settings_issues`): required values and platform rules become
  `Issue`s. A target with an error stays in `draft` and cannot be released, so
  a person (or agent) can save half-finished settings and come back.

Required with no default, because a platform default would decide for the
creator: `privacy` (YouTube), `made_for_kids` and `has_paid_product_placement`
(YouTube; since 2026-11-16 an omitted placement flag means "not declared
free of paid placement"), `privacy_level` (TikTok, no preselection). Privacy is
explicit everywhere it exists (invariant 13); Instagram has no privacy field.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from runway.platforms.publisher import Issue
from runway.posts.types import TargetPostType

# Structural caps: generous bounds that keep stored JSON small. The platform
# limits below are checked as issues so a draft can still be saved.
_TEXT_CAP = 20_000
_LIST_CAP = 50


class TargetSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    def effective(self, *, ai_generated: bool) -> dict[str, Any]:
        """The values that will be sent, with Runway's own defaults applied."""
        return self.model_dump(mode="json")


class YouTubeVideoSettings(TargetSettings):
    """`youtube_video` and `youtube_short` (the same API call; spec 08 section 2)."""

    title: str | None = Field(default=None, max_length=1000)
    description: str | None = Field(default=None, max_length=_TEXT_CAP)
    tags: list[str] = Field(default_factory=list, max_length=_LIST_CAP)
    category_id: str | None = Field(default=None, max_length=10)
    privacy: Literal["public", "unlisted", "private"] | None = None
    made_for_kids: bool | None = None
    has_paid_product_placement: bool | None = None
    contains_synthetic_media: bool | None = None
    notify_subscribers: bool | None = None
    embeddable: bool | None = None
    license: Literal["youtube", "creativeCommon"] | None = None
    schedule_mode: Literal["native", "held"] | None = None
    thumbnail_asset_id: int | None = Field(default=None, ge=1)

    def effective(self, *, ai_generated: bool) -> dict[str, Any]:
        values = self.model_dump(mode="json")
        if values["contains_synthetic_media"] is None:
            values["contains_synthetic_media"] = ai_generated
        if values["notify_subscribers"] is None:
            values["notify_subscribers"] = True
        if values["schedule_mode"] is None:
            values["schedule_mode"] = "native"
        return values


class YouTubePostSettings(TargetSettings):
    """`youtube_post`: the text is the target caption; images are the post media."""


class InstagramReelSettings(TargetSettings):
    share_to_feed: bool | None = None
    cover_asset_id: int | None = Field(default=None, ge=1)
    thumb_offset_ms: int | None = Field(default=None, ge=0)
    audio_name: str | None = Field(default=None, max_length=500)
    collaborators: list[str] = Field(default_factory=list, max_length=_LIST_CAP)
    trial: Literal["MANUAL", "SS_PERFORMANCE"] | None = None
    is_ai_generated: bool | None = None

    def effective(self, *, ai_generated: bool) -> dict[str, Any]:
        values = self.model_dump(mode="json")
        if values["share_to_feed"] is None:
            values["share_to_feed"] = True
        if values["is_ai_generated"] is None:
            values["is_ai_generated"] = ai_generated
        return values


class InstagramUserTag(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    username: str = Field(min_length=1, max_length=100)
    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class InstagramImageSettings(TargetSettings):
    alt_text: str | None = Field(default=None, max_length=_TEXT_CAP)
    user_tags: list[InstagramUserTag] = Field(default_factory=list, max_length=_LIST_CAP)
    location_id: str | None = Field(default=None, max_length=100)
    collaborators: list[str] = Field(default_factory=list, max_length=_LIST_CAP)
    is_ai_generated: bool | None = None

    def effective(self, *, ai_generated: bool) -> dict[str, Any]:
        values = self.model_dump(mode="json")
        if values["is_ai_generated"] is None:
            values["is_ai_generated"] = ai_generated
        return values


class InstagramCarouselSettings(TargetSettings):
    """Caption and `is_ai_generated` live on the parent only (children have none)."""

    location_id: str | None = Field(default=None, max_length=100)
    collaborators: list[str] = Field(default_factory=list, max_length=_LIST_CAP)
    is_ai_generated: bool | None = None

    def effective(self, *, ai_generated: bool) -> dict[str, Any]:
        values = self.model_dump(mode="json")
        if values["is_ai_generated"] is None:
            values["is_ai_generated"] = ai_generated
        return values


class InstagramStorySettings(TargetSettings):
    """Stories have no caption and no stickers through the API."""


TikTokPrivacy = Literal[
    "PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "FOLLOWER_OF_CREATOR", "SELF_ONLY"
]


class TikTokVideoSettings(TargetSettings):
    """TikTok's mandatory posting screen (research/tiktok section 7).

    Interaction toggles start off and commercial content is off; `privacy_level`
    has no preselection. Only a person may release these (spec 06 section 4).
    """

    privacy_level: TikTokPrivacy | None = None
    allow_comment: bool = False
    allow_duet: bool = False
    allow_stitch: bool = False
    commercial_content: bool = False
    your_brand: bool = False
    branded_content: bool = False
    is_aigc: bool | None = None
    cover_timestamp_ms: int | None = Field(default=None, ge=0)

    def effective(self, *, ai_generated: bool) -> dict[str, Any]:
        values = self.model_dump(mode="json")
        if values["is_aigc"] is None:
            values["is_aigc"] = ai_generated
        if not values["commercial_content"]:
            values["your_brand"] = False
            values["branded_content"] = False
        return values


class TikTokDraftSettings(TargetSettings):
    """Inbox drafts carry no post settings; the creator sets everything in TikTok."""


SETTINGS_MODELS: dict[TargetPostType, type[TargetSettings]] = {
    TargetPostType.YOUTUBE_VIDEO: YouTubeVideoSettings,
    TargetPostType.YOUTUBE_SHORT: YouTubeVideoSettings,
    TargetPostType.YOUTUBE_POST: YouTubePostSettings,
    TargetPostType.INSTAGRAM_REEL: InstagramReelSettings,
    TargetPostType.INSTAGRAM_IMAGE: InstagramImageSettings,
    TargetPostType.INSTAGRAM_CAROUSEL: InstagramCarouselSettings,
    TargetPostType.INSTAGRAM_STORY: InstagramStorySettings,
    TargetPostType.TIKTOK_VIDEO: TikTokVideoSettings,
    TargetPostType.TIKTOK_DRAFT: TikTokDraftSettings,
}

# Post types with a privacy field, and its name (invariant 13).
PRIVACY_FIELDS: dict[TargetPostType, str] = {
    TargetPostType.YOUTUBE_VIDEO: "privacy",
    TargetPostType.YOUTUBE_SHORT: "privacy",
    TargetPostType.TIKTOK_VIDEO: "privacy_level",
}


class InvalidTargetSettings(ValueError):
    def __init__(self, post_type: str, errors: list[dict[str, Any]]) -> None:
        super().__init__(f"settings are not valid for {post_type}")
        self.post_type = post_type
        self.errors = errors


def parse_settings(
    post_type: TargetPostType | str, raw: Mapping[str, Any] | None
) -> TargetSettings:
    """Structural validation; raises `InvalidTargetSettings` (loc/msg/type only)."""
    kind = TargetPostType(post_type)
    model = SETTINGS_MODELS[kind]
    try:
        return model.model_validate(dict(raw or {}))
    except ValidationError as exc:
        errors = [
            {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]
        raise InvalidTargetSettings(kind.value, errors) from None


def explicit_privacy(post_type: TargetPostType | str, settings: TargetSettings) -> bool:
    """True when the post type has no privacy field or it is set explicitly."""
    field_name = PRIVACY_FIELDS.get(TargetPostType(post_type))
    return field_name is None or getattr(settings, field_name, None) is not None


_HASHTAG = re.compile(r"(?<![\w&])#\w+")
_MENTION = re.compile(r"(?<![\w.])@[\w.]+")

YOUTUBE_TITLE_MAX = 100
YOUTUBE_DESCRIPTION_MAX = 5000
YOUTUBE_TAGS_TOTAL_MAX = 500
INSTAGRAM_CAPTION_MAX = 2200
INSTAGRAM_HASHTAGS_MAX = 30
INSTAGRAM_MENTIONS_MAX = 20
INSTAGRAM_COLLABORATORS_MAX = 3
INSTAGRAM_ALT_TEXT_MAX = 1000
TIKTOK_TITLE_MAX_UTF16 = 2200


def _required(field_name: str, message: str, *, code: str = "field_required") -> Issue:
    return Issue("error", code, message, field=field_name, source="spec 08")


def _utf16_length(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def settings_issues(
    post_type: TargetPostType | str,
    settings: TargetSettings,
    *,
    caption: str,
) -> list[Issue]:
    """Completeness and platform-rule issues for one target's settings and caption."""
    kind = TargetPostType(post_type)
    issues: list[Issue] = []
    if isinstance(settings, YouTubeVideoSettings):
        issues.extend(_youtube_issues(settings, caption))
    elif isinstance(settings, InstagramReelSettings | InstagramImageSettings):
        issues.extend(_instagram_caption_issues(caption))
        issues.extend(_collaborator_issues(settings.collaborators))
        if isinstance(settings, InstagramReelSettings):
            if settings.cover_asset_id is not None and settings.thumb_offset_ms is not None:
                issues.append(
                    Issue(
                        "error",
                        "cover_conflict",
                        "choose either a cover image or a thumbnail offset, not both",
                        field="cover_asset_id",
                    )
                )
        elif settings.alt_text is not None and len(settings.alt_text) > INSTAGRAM_ALT_TEXT_MAX:
            issues.append(
                Issue(
                    "error",
                    "alt_text_too_long",
                    f"alt text is limited to {INSTAGRAM_ALT_TEXT_MAX} characters",
                    field="alt_text",
                )
            )
    elif isinstance(settings, InstagramCarouselSettings):
        issues.extend(_instagram_caption_issues(caption))
        issues.extend(_collaborator_issues(settings.collaborators))
    elif kind is TargetPostType.INSTAGRAM_STORY:
        if caption.strip():
            issues.append(
                Issue(
                    "warning",
                    "caption_not_sent",
                    "Instagram Stories have no caption; the caption is not sent",
                )
            )
    elif isinstance(settings, TikTokVideoSettings):
        issues.extend(_tiktok_issues(settings, caption))
    elif kind is TargetPostType.TIKTOK_DRAFT and _utf16_length(caption) > TIKTOK_TITLE_MAX_UTF16:
        issues.append(_tiktok_title_issue())
    return issues


def _youtube_issues(settings: YouTubeVideoSettings, caption: str) -> list[Issue]:
    issues: list[Issue] = []
    title = (settings.title or "").strip()
    if not title:
        issues.append(_required("title", "a YouTube video needs a title"))
    elif len(title) > YOUTUBE_TITLE_MAX:
        issues.append(
            Issue(
                "error",
                "title_too_long",
                f"YouTube titles are limited to {YOUTUBE_TITLE_MAX} characters",
                field="title",
            )
        )
    if "<" in title or ">" in title:
        issues.append(
            Issue("error", "title_invalid", "YouTube titles cannot contain < or >", field="title")
        )
    description = settings.description if settings.description is not None else caption
    if len(description) > YOUTUBE_DESCRIPTION_MAX:
        issues.append(
            Issue(
                "error",
                "description_too_long",
                f"YouTube descriptions are limited to {YOUTUBE_DESCRIPTION_MAX} characters",
                field="description",
            )
        )
    if sum(len(tag) for tag in settings.tags) > YOUTUBE_TAGS_TOTAL_MAX:
        issues.append(
            Issue(
                "error",
                "tags_too_long",
                f"YouTube tags are limited to {YOUTUBE_TAGS_TOTAL_MAX} characters in total",
                field="tags",
            )
        )
    if settings.category_id is None:
        issues.append(_required("category_id", "choose a YouTube category"))
    elif not settings.category_id.isdigit():
        issues.append(
            Issue(
                "error",
                "invalid_category_id",
                "the category must be a YouTube category id",
                field="category_id",
            )
        )
    if settings.privacy is None:
        issues.append(
            _required(
                "privacy",
                "choose who can see the video (public, unlisted or private); Runway never "
                "relies on YouTube's default",
                code="privacy_required",
            )
        )
    if settings.made_for_kids is None:
        issues.append(_required("made_for_kids", "say whether the video is made for kids"))
    if settings.has_paid_product_placement is None:
        issues.append(
            _required(
                "has_paid_product_placement",
                "say whether the video contains paid product placement",
            )
        )
    return issues


def _instagram_caption_issues(caption: str) -> list[Issue]:
    issues: list[Issue] = []
    if len(caption) > INSTAGRAM_CAPTION_MAX:
        issues.append(
            Issue(
                "error",
                "caption_too_long",
                f"Instagram captions are limited to {INSTAGRAM_CAPTION_MAX} characters",
                field="caption",
            )
        )
    if len(_HASHTAG.findall(caption)) > INSTAGRAM_HASHTAGS_MAX:
        issues.append(
            Issue(
                "error",
                "too_many_hashtags",
                f"Instagram allows at most {INSTAGRAM_HASHTAGS_MAX} hashtags",
                field="caption",
            )
        )
    if len(_MENTION.findall(caption)) > INSTAGRAM_MENTIONS_MAX:
        issues.append(
            Issue(
                "error",
                "too_many_mentions",
                f"Instagram allows at most {INSTAGRAM_MENTIONS_MAX} mentions",
                field="caption",
            )
        )
    return issues


def _collaborator_issues(collaborators: list[str]) -> list[Issue]:
    if len(collaborators) <= INSTAGRAM_COLLABORATORS_MAX:
        return []
    return [
        Issue(
            "error",
            "too_many_collaborators",
            f"Instagram allows at most {INSTAGRAM_COLLABORATORS_MAX} collaborators",
            field="collaborators",
        )
    ]


def _tiktok_title_issue() -> Issue:
    return Issue(
        "error",
        "caption_too_long",
        f"TikTok captions are limited to {TIKTOK_TITLE_MAX_UTF16} UTF-16 characters",
        field="caption",
    )


def _tiktok_issues(settings: TikTokVideoSettings, caption: str) -> list[Issue]:
    issues: list[Issue] = []
    if _utf16_length(caption) > TIKTOK_TITLE_MAX_UTF16:
        issues.append(_tiktok_title_issue())
    if settings.privacy_level is None:
        issues.append(
            _required(
                "privacy_level",
                "choose who can see the video; TikTok requires the creator to pick it",
                code="privacy_required",
            )
        )
    if settings.commercial_content and not (settings.your_brand or settings.branded_content):
        issues.append(
            Issue(
                "error",
                "commercial_content_incomplete",
                "You need to indicate if your content promotes yourself, a third party, or both.",
                field="commercial_content",
                source="TikTok content sharing guidelines",
            )
        )
    if (
        settings.commercial_content
        and settings.branded_content
        and settings.privacy_level == "SELF_ONLY"
    ):
        issues.append(
            Issue(
                "error",
                "branded_content_private",
                "Branded content visibility cannot be set to private.",
                field="privacy_level",
                source="TikTok content sharing guidelines",
            )
        )
    return issues


def release_notice(post_type: TargetPostType | str, settings: TargetSettings) -> str | None:
    """Text the release dialog must show above the release button."""
    kind = TargetPostType(post_type)
    if kind is TargetPostType.TIKTOK_DRAFT:
        return "This sends a draft to your TikTok inbox. You finish and post it in the app."
    if isinstance(settings, TikTokVideoSettings):
        if settings.commercial_content and settings.branded_content:
            return (
                "By posting, you agree to TikTok's Branded Content Policy and Music Usage "
                "Confirmation."
            )
        return "By posting, you agree to TikTok's Music Usage Confirmation"
    if kind.value.startswith("instagram_"):
        return "Visible to everyone who can see this account."
    if kind is TargetPostType.YOUTUBE_POST:
        return "Community posts are public on the channel."
    return None


def privacy_in_words(post_type: TargetPostType | str, settings: TargetSettings) -> str:
    """The privacy setting as the release dialog states it."""
    kind = TargetPostType(post_type)
    if isinstance(settings, YouTubeVideoSettings):
        return {
            "public": "Public: anyone can find and watch it",
            "unlisted": "Unlisted: anyone with the link can watch it",
            "private": "Private: only you can watch it",
            None: "Not chosen yet",
        }[settings.privacy]
    if isinstance(settings, TikTokVideoSettings):
        return {
            "PUBLIC_TO_EVERYONE": "Everyone",
            "MUTUAL_FOLLOW_FRIENDS": "Friends (followers you follow back)",
            "FOLLOWER_OF_CREATOR": "Followers",
            "SELF_ONLY": "Only you",
            None: "Not chosen yet",
        }[settings.privacy_level]
    if kind is TargetPostType.TIKTOK_DRAFT:
        return "Chosen by you in the TikTok app"
    if kind.value.startswith("instagram_"):
        return "Visible to everyone who can see this account"
    return "Public on the channel"
