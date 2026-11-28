"""The `youtube_post` adapter: YouTube Community posts through the `posts` API.

Spec 08 section 2 "Community posts" (added 2026-11-22; the API is a
launch-partner pilot, so `Settings.youtube_posts_api_access` stays off until
Google admits the project, and the capability reports `needs_app_approval`
meanwhile - the Lineup browser path remains the default):

- Preflight: the pilot flag is on, a connected account holds `youtube.force-ssl`
  (or `youtube`), media are images only (0-10, JPEG/PNG/GIF/WebP, <= 16 MB,
  aspect 2:5-5:2) and a media-less post still has text. The adapter also runs
  the "never both" guard of `runway.posts.lineup_guard`: a proposal already
  claimed by the browser publisher, or by another released `youtube_post`
  target on a channel this target cannot be proven different from, is refused.
- Send: one `posts.imageUpload` per image (`channelId` query param, raw bytes,
  keep the `imageUploadToken`; a fresh upload per send - tokens are never
  reused, not even within a retried attempt), then `posts.insert` carries
  `snippet.channelId`, `snippet.textDisplay` and the tokens.
- Point of no return: `ctx.mark_request_sent()` immediately before
  `posts.insert` - the only request that can create the post.
- Poll/verify: `posts.list` by id; verify compares the text and the image
  count it read back. Reconcile matches the channel's newest posts by text and
  image count within the attempt window, as for uploads.
- Identity check before every write: `channels.list?mine=true` must return the
  connected channel (`wrong_channel` otherwise).
- Errors reuse the YouTube taxonomy: auth reasons and `401` are
  `auth_revoked`, `quotaExceeded`-family and `429` retry after Pacific
  midnight, `5xx`/retryable statuses retry shortly, everything else is a final
  refusal. There is no scheduling ("Scheduled posts aren't supported"): the
  engine holds the target until `planned_at` and the insert publishes
  immediately.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, BinaryIO

import httpx

from runway.config import Settings
from runway.db.base import Database
from runway.platforms.base import TokenSet, provider_request
from runway.platforms.capabilities import Platform
from runway.platforms.limits import (
    YOUTUBE_POST_IMAGE,
    YOUTUBE_POST_IMAGE_TYPES,
    YOUTUBE_POST_QUOTA,
    YOUTUBE_UPLOAD,
)
from runway.platforms.post_settings import InvalidTargetSettings, parse_settings
from runway.platforms.publish_capability import REQUIRED_SCOPES
from runway.platforms.publisher import (
    Issue,
    MediaRef,
    PollResult,
    Progress,
    PublishAuthRevoked,
    PublishContext,
    PublishError,
    PublishRefused,
    PublishRetryLater,
    ReconcileResult,
    SendResult,
    VerifyResult,
    pacific_day_limit,
)
from runway.platforms.youtube import API_BASE, YouTubeProvider
from runway.platforms.youtube_direct import (
    _AUTH_REASONS,
    _QUOTA_REASONS,
    RECONCILE_LOOKBACK,
    SESSION_RETRY_DELAY,
    _as_dict,
    _error_details,
    _next_pacific_midnight,
    _parse_rfc3339,
)
from runway.posts.lineup_guard import (
    browser_path_claimed,
    channel_conflict,
    claimed_api_targets,
    proposal_for_post,
)
from runway.posts.types import TargetPostType

UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3"
INSERT_PARTS = "snippet,attachments"
LIST_PARTS = "snippet,attachments"
SUPPORTED_TYPES = frozenset({TargetPostType.YOUTUBE_POST.value})
RECONCILE_PAGE = 10
_POST_SOURCE = "YouTube posts API (pilot), read 2026-11-22"


def _post_url(post_id: str) -> str:
    return f"https://www.youtube.com/post/{post_id}"


def _image_count(item: dict[str, Any]) -> int:
    attachments = _as_dict(item.get("attachments"))
    images = attachments.get("imageAttachments")
    return len(images) if isinstance(images, list) else 0


class YouTubePostPublisher:
    """`youtube_post` through `posts.imageUpload` + `posts.insert` (spec 08 section 2)."""

    name = "youtube_post"
    kind = "direct"
    platform = Platform.youtube
    # `posts.insert` draws on a separate "Post Uploads" bucket; Runway stays
    # under it with at most a few community posts per channel per Pacific day
    # (YOUTUBE_POST_QUOTA in platforms/limits).
    rate_guards = (
        pacific_day_limit(YOUTUBE_POST_QUOTA.max_count, scope="account", label="community posts"),
    )

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        # Image uploads carry up to 16 MB each: generous read timeout, short connects.
        self.client = client or httpx.Client(timeout=httpx.Timeout(30.0, read=120.0))
        self._provider = YouTubeProvider(settings, self.client)

    # -------------------------------------------------------------- helpers

    def supports(self, post_type: str) -> bool:
        return post_type in SUPPORTED_TYPES

    def _bearer(self, token: TokenSet) -> dict[str, str]:
        return {"Authorization": f"Bearer {token.access_token}"}

    def _api_get(self, token: TokenSet, params: dict[str, Any]) -> dict[str, Any]:
        response = provider_request(
            self.client,
            "YouTube",
            "GET",
            f"{API_BASE}/posts",
            params=params,
            headers=self._bearer(token),
        )
        return self._provider._json_or_raise(response)

    def _check_channel(self, ctx: PublishContext, token: TokenSet) -> None:
        """Identity check before every write (spec 08 section 1)."""
        if ctx.account is None:
            raise PublishRefused("account_required", "choose a connected YouTube channel")
        identity = self._provider.fetch_identity(token)
        if identity.external_account_id != ctx.account.external_account_id:
            raise PublishError(
                "wrong_channel",
                "the token is for channel "
                f"{identity.external_account_id}, not the connected channel "
                f"{ctx.account.external_account_id}; reconnect the account",
                final=True,
                delivered=False,
            )

    def _post_items(self, token: TokenSet, params: dict[str, Any]) -> list[dict[str, Any]]:
        data = self._api_get(token, {"part": LIST_PARTS, **params})
        items = data.get("items")
        if not isinstance(items, list):
            return []
        return [dict(item) for item in items if isinstance(item, dict)]

    def _raise_for_error(self, response: httpx.Response, now: datetime) -> None:
        """Map a failed write response to a typed PublishError."""
        message, reasons = _error_details(response)
        if response.status_code == 401 or reasons & _AUTH_REASONS:
            raise PublishAuthRevoked(f"{message}; reconnect the YouTube account")
        if reasons & _QUOTA_REASONS or response.status_code == 429:
            raise PublishRetryLater(
                "quota_exceeded",
                f"{message}; the YouTube quota resets at Pacific midnight",
                retry_after=_next_pacific_midnight(now),
            )
        if response.status_code in YOUTUBE_UPLOAD.retryable_statuses or (
            response.status_code >= 500
        ):
            raise PublishRetryLater(
                "youtube_server_error",
                f"{message}; trying later is safe",
                retry_after=now + SESSION_RETRY_DELAY,
            )
        raise PublishRefused(
            "post_refused", f"YouTube refused the post ({response.status_code}): {message}"
        )

    def _expected_images(self, ctx: PublishContext) -> int:
        return len([media for media in ctx.media if media.kind == "image"])

    def _recent_matches(self, ctx: PublishContext, token: TokenSet) -> list[dict[str, Any]]:
        """Newest channel posts matching this attempt by text, image count and time."""
        channel = ctx.account.external_account_id if ctx.account is not None else ""
        items = self._post_items(token, {"channelId": channel, "maxResults": RECONCILE_PAGE})
        anchor = ctx.request_sent_at or ctx.planned_at or ctx.now()
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=UTC)
        since = anchor - RECONCILE_LOOKBACK
        images = self._expected_images(ctx)
        matches: list[dict[str, Any]] = []
        for item in items:
            snippet = _as_dict(item.get("snippet"))
            if snippet.get("textDisplay") != ctx.caption:
                continue
            if _image_count(item) != images:
                continue
            published_at = _parse_rfc3339(snippet.get("publishedAt"))
            if published_at is not None and published_at < since:
                continue
            matches.append(item)
        return matches

    # -------------------------------------------------------------- preflight

    def preflight(self, ctx: PublishContext) -> list[Issue]:
        issues: list[Issue] = []
        if ctx.post_type not in SUPPORTED_TYPES or ctx.platform is not Platform.youtube:
            return [Issue("error", "unsupported", f"youtube_post cannot publish {ctx.post_type}")]
        if not self.settings.youtube_posts_api_access:
            issues.append(
                Issue(
                    "error",
                    "posts_api_not_enabled",
                    "the YouTube posts API is a pilot; set youtube_posts_api_access once "
                    "Google admits the project (the Lineup browser path stays available)",
                    source=_POST_SOURCE,
                )
            )
        if ctx.account is None:
            issues.append(
                Issue(
                    "error",
                    "account_required",
                    "choose a connected YouTube channel",
                    field="account_id",
                )
            )
        else:
            if ctx.account.status != "connected":
                issues.append(
                    Issue(
                        "error",
                        "account_not_connected",
                        f"the channel is {ctx.account.status}; reconnect it",
                        field="account_id",
                    )
                )
            if not ctx.account.token_ref:
                issues.append(
                    Issue(
                        "error",
                        "no_token",
                        "the channel has no stored token; reconnect it",
                        field="account_id",
                    )
                )
            wanted = set(REQUIRED_SCOPES[TargetPostType.YOUTUBE_POST])
            if not wanted & set(ctx.account.granted_scopes):
                issues.append(
                    Issue(
                        "error",
                        "missing_scope",
                        "turn on publishing for this channel to grant the posts scope "
                        f"({REQUIRED_SCOPES[TargetPostType.YOUTUBE_POST][0]})",
                        fix="POST /api/v1/accounts/{id}/publishing/enable, then try again",
                        field="account_id",
                    )
                )
        try:
            parse_settings(TargetPostType.YOUTUBE_POST, dict(ctx.settings))
        except InvalidTargetSettings:
            issues.append(
                Issue(
                    "error",
                    "settings_invalid",
                    "the stored settings do not fit this post type; set them again",
                    field="settings",
                )
            )
        issues.extend(self._media_issues(ctx))
        issues.extend(self._lineup_issues(ctx))
        return issues

    def _media_issues(self, ctx: PublishContext) -> list[Issue]:
        issues: list[Issue] = []
        images = [media for media in ctx.media if media.kind == "image"]
        for media in ctx.media:
            if media.kind != "image":
                issues.append(
                    Issue(
                        "error",
                        "media_kind_not_supported",
                        "a Community post takes images only",
                        item=media.position,
                    )
                )
        if len(ctx.media) > 10:
            issues.append(
                Issue(
                    "error",
                    "media_count",
                    f"a Community post takes at most 10 images; this post has {len(ctx.media)}",
                )
            )
        if not images and not ctx.caption.strip():
            issues.append(
                Issue(
                    "error",
                    "post_empty",
                    "a Community post needs text or at least one image",
                )
            )
        low, high = YOUTUBE_POST_IMAGE.min_aspect or 0.0, YOUTUBE_POST_IMAGE.max_aspect
        for media in images:
            if media.content_type not in YOUTUBE_POST_IMAGE_TYPES:
                issues.append(
                    Issue(
                        "error",
                        "image_format",
                        "Community-post images must be JPEG, PNG, GIF or WebP",
                        item=media.position,
                        source=_POST_SOURCE,
                    )
                )
            if media.size_bytes <= 0 or media.size_bytes > YOUTUBE_POST_IMAGE.max_size_bytes:
                issues.append(
                    Issue(
                        "error",
                        "file_too_large",
                        "Community-post images are limited to 16 MB",
                        item=media.position,
                        source=_POST_SOURCE,
                    )
                )
            width = media.probe.get("width")
            height = media.probe.get("height")
            if (
                isinstance(width, int | float)
                and isinstance(height, int | float)
                and width > 0
                and height > 0
            ):
                aspect = width / height
                if high is not None and not low <= aspect <= high:
                    issues.append(
                        Issue(
                            "error",
                            "aspect_ratio",
                            "Community-post images must be between 2:5 and 5:2",
                            item=media.position,
                            source=_POST_SOURCE,
                        )
                    )
            try:
                stream = ctx.open_media(media)
            except Exception:
                issues.append(
                    Issue(
                        "error",
                        "file_unreadable",
                        "the media file cannot be read; upload it again",
                        item=media.position,
                    )
                )
            else:
                stream.close()
        return issues

    def _lineup_issues(self, ctx: PublishContext) -> list[Issue]:
        """The "never both" guard (spec 08 section 2): refuse proposals another path claimed."""
        with self.database.session() as session:
            proposal = proposal_for_post(session, ctx.post_id)
            if proposal is None:
                return []
            account_channel = ctx.account.external_account_id if ctx.account is not None else None
            issues: list[Issue] = []
            if browser_path_claimed(session, proposal) and channel_conflict(
                account_channel, self.settings.publisher_channel_id
            ):
                issues.append(
                    Issue(
                        "error",
                        "lineup_browser_claimed",
                        "this proposal is already on its way through the browser publisher; "
                        "the posts API cannot publish it a second time",
                    )
                )
            if any(
                channel_conflict(
                    account.external_account_id if account is not None else None,
                    account_channel,
                )
                for _target, account in claimed_api_targets(
                    session, proposal.id, exclude_target_id=ctx.target_id
                )
            ):
                issues.append(
                    Issue(
                        "error",
                        "lineup_claimed",
                        "another youtube_post target already claimed this proposal on a "
                        "channel this one cannot be proven different from",
                    )
                )
            return issues

    # ------------------------------------------------------------------- send

    def _upload_image(
        self, ctx: PublishContext, token: TokenSet, channel_id: str, media: MediaRef
    ) -> str:
        stream: BinaryIO = ctx.open_media(media)
        try:
            body = stream.read()
        finally:
            stream.close()
        response = provider_request(
            self.client,
            "YouTube",
            "POST",
            f"{UPLOAD_BASE}/posts/image",
            params={"channelId": channel_id},
            headers={
                **self._bearer(token),
                "Content-Type": media.content_type or "image/jpeg",
            },
            content=body,
        )
        if not response.is_success:
            self._raise_for_error(response, ctx.now())
        upload_token = _as_dict(response.json() if response.content else {}).get("imageUploadToken")
        if not isinstance(upload_token, str) or not upload_token:
            raise PublishError(
                "upload_token_missing",
                "posts.imageUpload answered without an imageUploadToken",
                delivered=False,
            )
        return upload_token

    def send(self, ctx: PublishContext, progress: Progress) -> SendResult:
        token = ctx.access_token()
        self._check_channel(ctx, token)
        assert ctx.account is not None
        channel_id = ctx.account.external_account_id
        images = [media for media in ctx.media if media.kind == "image"]
        upload_tokens: list[str] = []
        for index, media in enumerate(images):
            progress.report(index, len(images), stage="uploading_images")
            upload_tokens.append(self._upload_image(ctx, token, channel_id, media))
        # The point of no return: everything before this cannot have created a
        # post (spec 03 section 4).
        ctx.mark_request_sent()
        body: dict[str, Any] = {"snippet": {"channelId": channel_id, "textDisplay": ctx.caption}}
        if upload_tokens:
            body["attachments"] = {
                "imageAttachments": [
                    {"uploadToken": upload_token} for upload_token in upload_tokens
                ]
            }
        response = provider_request(
            self.client,
            "YouTube",
            "POST",
            f"{API_BASE}/posts",
            params={"part": INSERT_PARTS},
            headers=self._bearer(token),
            json=body,
        )
        if not response.is_success:
            self._raise_for_error(response, ctx.now())
        data = _as_dict(response.json() if response.content else {})
        post_id = str(data.get("id") or "")
        if not post_id:
            raise PublishError(
                "post_id_missing",
                "posts.insert answered without a post id",
                delivered=None,
            )
        progress.report(len(images), len(images), stage="published")
        return SendResult(
            status="published",
            external_id=post_id,
            permalink=_post_url(post_id),
            response={"post_id": post_id},
        )

    # ------------------------------------------------------------------- poll

    def poll(self, ctx: PublishContext) -> PollResult:
        token = ctx.access_token()
        if ctx.external_id:
            items = self._post_items(token, {"id": ctx.external_id})
            if items:
                return PollResult(
                    status="published",
                    external_id=ctx.external_id,
                    permalink=_post_url(ctx.external_id),
                )
        return PollResult(status="processing", external_id=ctx.external_id)

    # ----------------------------------------------------------------- verify

    def verify(self, ctx: PublishContext) -> VerifyResult:
        if not ctx.external_id:
            return VerifyResult(verified=None, detail="there is no post id to read back")
        token = ctx.access_token()
        items = self._post_items(token, {"id": ctx.external_id})
        if not items:
            return VerifyResult(
                verified=None,
                permalink=_post_url(ctx.external_id),
                detail="the post is not visible to the connected channel yet",
            )
        item = items[0]
        snippet = _as_dict(item.get("snippet"))
        mismatches: list[str] = []
        text = str(snippet.get("textDisplay") or "")
        if text != ctx.caption:
            mismatches.append(f"text is {text!r}, expected {ctx.caption!r}")
        images = _image_count(item)
        expected = self._expected_images(ctx)
        if images != expected:
            mismatches.append(f"the post shows {images} image attachments, expected {expected}")
        return VerifyResult(
            verified=not mismatches,
            permalink=_post_url(ctx.external_id),
            mismatches=tuple(mismatches),
        )

    # --------------------------------------------------------------- reconcile

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        token = ctx.access_token()
        checked: list[str] = []
        if ctx.external_id:
            checked.append(f"posts.list for post {ctx.external_id}")
            items = self._post_items(token, {"id": ctx.external_id})
            if items:
                return ReconcileResult(
                    status="published",
                    external_id=ctx.external_id,
                    permalink=_post_url(ctx.external_id),
                    checked=tuple(checked),
                )
        checked.append(
            f"the channel's {RECONCILE_PAGE} newest posts, matched by text, "
            "image count and the attempt's time window"
        )
        matches = self._recent_matches(ctx, token)
        if len(matches) == 1:
            post_id = str(matches[0].get("id") or "")
            return ReconcileResult(
                status="published",
                external_id=post_id or None,
                permalink=_post_url(post_id) if post_id else None,
                checked=tuple(checked),
            )
        if not matches:
            return ReconcileResult(
                status="absent",
                detail="no matching Community post on the channel",
                checked=tuple(checked),
            )
        candidates = tuple(
            {
                "external_id": str(item.get("id") or ""),
                "permalink": _post_url(str(item.get("id") or "")),
                "published_at": _as_dict(item.get("snippet")).get("publishedAt"),
            }
            for item in matches
        )
        return ReconcileResult(
            status="uncertain",
            detail=f"{len(matches)} recent Community posts match this attempt",
            checked=tuple(checked),
            candidates=candidates,
        )
