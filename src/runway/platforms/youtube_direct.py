"""The `youtube_direct` adapter: real `youtube_video`/`youtube_short` uploads.

Spec 08 section 2 "Send":

- Preflight: the upload scope is granted, the file is readable and the
  settings are complete (privacy, made-for-kids and paid-placement are all
  explicit). The per-Google-project upload counter is the adapter's
  `pacific_day_limit` rate guard, checked at queue time and again at send
  time before this preflight runs.
- Upload: a resumable session (`part=snippet,status,paidProductPlacementDetails`),
  8 MB chunks through `runway.mediastore.delivery.iter_youtube_chunks`, with
  the session URI and the confirmed offset persisted on the attempt after
  every chunk (`ctx.save_resumable`). `native` scheduling uploads as
  `privacyStatus=private` with `publishAt`; `held` sends the chosen privacy.
- Point of no return: `ctx.mark_request_sent()` immediately before the PUT
  carrying the final byte range.
- Poll: `videos.list?part=status,processingDetails` until `processed`, then
  `thumbnails.set` (a `403` is a warning, never a failed post). `failed` and
  `rejected` are final with the platform's reason.
- Verify: read back title, privacy, `publishAt` and the paid-placement flag.
- Reconcile (spec text): query the session (`Content-Range: bytes */N`).
  `308` means incomplete and safe to resume; `404`/`410` means the session
  expired, so the channel's newest uploads are matched by title and duration
  within the attempt's time window. One match: `published`. None: `absent`
  (`failed_retryable`). Anything else - several candidates, or a title-only
  one whose duration cannot be verified - is `uncertain`, never `published`.
- Named errors (`uploadLimitExceeded`, `quotaExceeded`, `invalidPublishAt`,
  `forbiddenPrivacySetting`, `invalidTitle`, `invalidDescription`,
  `invalidTags`, `invalidCategoryId`) map to final or retryable with a
  user-facing message; `quotaExceeded` retries after Pacific midnight.
- Identity check before every write: `channels.list?mine=true` must return
  the connected channel (`wrong_channel` otherwise).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, BinaryIO

import httpx

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import MediaAssetV2
from runway.mediastore.delivery import (
    iter_youtube_chunks,
    youtube_resume_offset,
    youtube_status_query_headers,
)
from runway.platforms.base import (
    TokenSet,
    provider_request,
    safe_error_summary,
)
from runway.platforms.capabilities import Platform
from runway.platforms.limits import YOUTUBE_SHORTS_HEURISTIC, YOUTUBE_UPLOAD
from runway.platforms.post_settings import (
    InvalidTargetSettings,
    YouTubeVideoSettings,
    parse_settings,
    settings_issues,
)
from runway.platforms.publish_capability import REQUIRED_SCOPES
from runway.platforms.publisher import (
    PACIFIC,
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
from runway.posts.types import TargetPostType

UPLOAD_BASE = "https://www.googleapis.com/upload/youtube/v3"
# The parts the insert declares; paid placement is always declared because
# an omitted flag now means "not declared free of paid placement".
INSERT_PARTS = "snippet,status,paidProductPlacementDetails"
POLL_PARTS = "status,processingDetails"
VERIFY_PARTS = "snippet,status,paidProductPlacementDetails"
SUPPORTED_TYPES = frozenset(
    {TargetPostType.YOUTUBE_VIDEO.value, TargetPostType.YOUTUBE_SHORT.value}
)

POLL_SECONDS = 30
SESSION_RETRY_DELAY = timedelta(minutes=5)
# How far before `request_sent_at` an upload may have been created and still
# count as this attempt's candidate.
RECONCILE_LOOKBACK = timedelta(minutes=30)
RECONCILE_PAGE = 10
DURATION_TOLERANCE_S = 3
PUBLISH_AT_TOLERANCE = timedelta(seconds=90)
# A native publishAt must still be in the future when the session starts.
PUBLISH_AT_BUFFER = timedelta(minutes=1)

_FINAL_REASONS: dict[str, tuple[str, str]] = {
    "uploadLimitExceeded": (
        "upload_limit_exceeded",
        "the channel hit YouTube's daily upload limit; reschedule for tomorrow",
    ),
    "invalidPublishAt": (
        "invalid_publish_at",
        "YouTube rejected the scheduled time; choose a new time a few minutes ahead",
    ),
    "forbiddenPrivacySetting": (
        "forbidden_privacy",
        "YouTube refused that visibility for this channel; choose another",
    ),
    "invalidTitle": ("invalid_title", "YouTube rejected the title"),
    "invalidDescription": ("invalid_description", "YouTube rejected the description"),
    "invalidTags": ("invalid_tags", "YouTube rejected the tags"),
    "invalidCategoryId": (
        "invalid_category",
        "YouTube does not know that category; pick one from the category list",
    ),
    "mediaBodyRequired": (
        "media_body_required",
        "YouTube expected the video bytes and did not get them",
    ),
}
_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded"}
_AUTH_REASONS = {"authError", "invalidCredentials", "authorizationError"}
_SESSION_EXPIRED = frozenset({404, 410})


def _watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _as_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _next_pacific_midnight(now: datetime) -> datetime:
    local = now.astimezone(PACIFIC)
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight + timedelta(days=1)).astimezone(UTC)


def _parse_rfc3339(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _error_details(response: httpx.Response) -> tuple[str, set[str]]:
    """(message, reasons) from a YouTube error payload."""
    message = f"YouTube answered HTTP {response.status_code}"
    reasons: set[str] = set()
    try:
        data = response.json()
    except ValueError:
        data = {}
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        if isinstance(error.get("message"), str) and error["message"]:
            message = str(error["message"])
        entries = error.get("errors")
        if isinstance(entries, list):
            reasons = {
                str(entry["reason"])
                for entry in entries
                if isinstance(entry, dict) and entry.get("reason")
            }
    return message, reasons


class YouTubeDirectPublisher:
    """`youtube_video` and `youtube_short` through videos.insert (spec 08 section 2)."""

    name = "youtube_direct"
    kind = "direct"
    platform = Platform.youtube
    # videos.insert has its own bucket: 100 calls per Google Cloud project per
    # Pacific day, shared by every account and workspace in the install.
    rate_guards = (pacific_day_limit(100, scope="platform", label="uploads"),)

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        # Chunk PUTs carry 8 MB each: the read timeout must survive a slow
        # uplink, while connects stay short.
        self.client = client or httpx.Client(timeout=httpx.Timeout(30.0, read=300.0))
        # Shares the client so reads (identity, uploads list) use the same
        # transport and error taxonomy as the writes below.
        self._provider = YouTubeProvider(settings, self.client)

    # -------------------------------------------------------------- helpers

    def supports(self, post_type: str) -> bool:
        return post_type in SUPPORTED_TYPES

    def _settings(self, ctx: PublishContext) -> YouTubeVideoSettings:
        settings = parse_settings(ctx.post_type, ctx.settings)
        if not isinstance(settings, YouTubeVideoSettings):
            raise TypeError(f"{ctx.post_type} settings are not YouTubeVideoSettings")
        return settings

    def _bearer(self, token: TokenSet) -> dict[str, str]:
        return {"Authorization": f"Bearer {token.access_token}"}

    def _api_get(self, token: TokenSet, url: str, params: dict[str, Any]) -> dict[str, Any]:
        response = provider_request(
            self.client,
            "YouTube",
            "GET",
            url,
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

    def _persist(self, ctx: PublishContext, **state: object) -> None:
        merged = dict(ctx.resumable)
        merged.update(state)
        ctx.save_resumable(merged)

    def _publish_at(self, ctx: PublishContext, settings: YouTubeVideoSettings) -> datetime | None:
        if (settings.schedule_mode or "native") != "native" or ctx.planned_at is None:
            return None
        planned = ctx.planned_at
        return planned if planned.tzinfo else planned.replace(tzinfo=UTC)

    def _metadata(
        self,
        ctx: PublishContext,
        settings: YouTubeVideoSettings,
        publish_at: datetime | None,
    ) -> dict[str, Any]:
        snippet: dict[str, Any] = {
            "title": settings.title or ctx.title,
            "description": (
                settings.description if settings.description is not None else ctx.caption
            ),
            "categoryId": settings.category_id,
        }
        if settings.tags:
            snippet["tags"] = list(settings.tags)
        status: dict[str, Any] = {
            # Native scheduling uploads as private; YouTube makes the video
            # public at publishAt itself (research/youtube section 3).
            "privacyStatus": "private" if publish_at else settings.privacy,
            "selfDeclaredMadeForKids": settings.made_for_kids,
            "containsSyntheticMedia": settings.contains_synthetic_media,
        }
        if publish_at is not None:
            status["publishAt"] = publish_at.strftime("%Y-%m-%dT%H:%M:%S.000Z")
        if settings.embeddable is not None:
            status["embeddable"] = settings.embeddable
        if settings.license:
            status["license"] = settings.license
        return {
            "snippet": snippet,
            "status": status,
            "paidProductPlacementDetails": {
                "hasPaidProductPlacement": settings.has_paid_product_placement
            },
        }

    def _raise_for_error(self, response: httpx.Response, now: datetime) -> None:
        """Map a failed upload-protocol response to a typed PublishError."""
        message, reasons = _error_details(response)
        if response.status_code == 401 or reasons & _AUTH_REASONS:
            raise PublishAuthRevoked(f"{message}; reconnect the YouTube account")
        if "uploadLimitExceeded" in reasons:
            code, hint = _FINAL_REASONS["uploadLimitExceeded"]
            raise PublishError(code, f"{hint} (YouTube: {message})", final=True, delivered=False)
        if reasons & _QUOTA_REASONS or response.status_code == 429:
            raise PublishRetryLater(
                "quota_exceeded",
                f"{message}; the YouTube quota resets at Pacific midnight",
                retry_after=_next_pacific_midnight(now),
            )
        for reason, (code, hint) in _FINAL_REASONS.items():
            if reason in reasons:
                raise PublishRefused(code, f"{hint} (YouTube: {message})")
        if response.status_code in YOUTUBE_UPLOAD.retryable_statuses or response.status_code >= 500:
            raise PublishRetryLater(
                "youtube_server_error",
                f"{message}; trying later is safe",
                retry_after=now + SESSION_RETRY_DELAY,
            )
        raise PublishRefused(
            "youtube_refused", f"YouTube refused the upload ({response.status_code}): {message}"
        )

    # -------------------------------------------------------------- preflight

    def preflight(self, ctx: PublishContext) -> list[Issue]:
        issues: list[Issue] = []
        if ctx.post_type not in SUPPORTED_TYPES or ctx.platform is not Platform.youtube:
            issues.append(
                Issue("error", "unsupported", f"youtube_direct cannot publish {ctx.post_type}")
            )
            return issues
        if ctx.account is None:
            issues.append(Issue("error", "account_required", "choose a connected YouTube channel"))
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
            required = set(REQUIRED_SCOPES[TargetPostType(ctx.post_type)])
            if not required & set(ctx.account.granted_scopes):
                issues.append(
                    Issue(
                        "error",
                        "missing_scope",
                        "turn on publishing for this channel to grant the upload scope",
                        fix="POST /api/v1/accounts/{id}/publishing/enable, then try again",
                        field="account_id",
                    )
                )
        settings: YouTubeVideoSettings | None = None
        try:
            settings = self._settings(ctx)
        except (InvalidTargetSettings, TypeError):
            issues.append(
                Issue(
                    "error",
                    "settings_invalid",
                    "the stored settings do not fit this post type; set them again",
                    field="settings",
                )
            )
        if settings is not None:
            issues.extend(settings_issues(ctx.post_type, settings, caption=ctx.caption))
        videos = [media for media in ctx.media if media.kind in {None, "video"}]
        if len(ctx.media) != 1 or len(videos) != 1:
            issues.append(Issue("error", "media_count", f"{ctx.post_type} takes exactly one video"))
        else:
            video = videos[0]
            if video.size_bytes <= 0 or video.size_bytes > YOUTUBE_UPLOAD.max_size_bytes:
                issues.append(
                    Issue(
                        "error",
                        "media_size",
                        "the video is empty or larger than YouTube accepts",
                        item=0,
                    )
                )
            try:
                stream = ctx.open_media(video)
                try:
                    stream.read(1)
                finally:
                    stream.close()
            except Exception as exc:  # noqa: BLE001 - any read failure blocks release
                issues.append(
                    Issue(
                        "error",
                        "file_unreadable",
                        f"the video file cannot be read: {safe_error_summary(exc)}",
                        item=0,
                    )
                )
        if settings is not None:
            publish_at = self._publish_at(ctx, settings)
            if publish_at is not None:
                if settings.privacy != "public":
                    issues.append(
                        Issue(
                            "error",
                            "native_needs_public",
                            "native scheduling makes the video public at the planned time; "
                            "choose public, or held mode for another visibility",
                            field="privacy",
                        )
                    )
                if publish_at <= ctx.now() + PUBLISH_AT_BUFFER:
                    issues.append(
                        Issue(
                            "error",
                            "publish_at_passed",
                            "the planned time passed; YouTube cannot schedule in the past - "
                            "choose a new time or held mode",
                            field="planned_at",
                        )
                    )
            if ctx.post_type == TargetPostType.YOUTUBE_SHORT.value and ctx.media:
                issues.extend(self._shorts_issues(ctx.media[0]))
            if settings.thumbnail_asset_id is not None:
                issues.extend(self._thumbnail_issues(ctx, settings.thumbnail_asset_id))
        return issues

    def _shorts_issues(self, video: MediaRef) -> list[Issue]:
        probe = dict(video.probe)
        duration_ms = probe.get("duration_ms")
        width, height = probe.get("width"), probe.get("height")
        too_long = (
            isinstance(duration_ms, int | float)
            and duration_ms > YOUTUBE_SHORTS_HEURISTIC.max_duration_ms
        )
        landscape = (
            isinstance(width, int | float)
            and isinstance(height, int | float)
            and height > 0
            and width / height > YOUTUBE_SHORTS_HEURISTIC.max_aspect
        )
        if not (too_long or landscape):
            return []
        return [
            Issue(
                "warning",
                "shorts_heuristic",
                "Runway's Shorts check is a heuristic: YouTube may not treat a file that is "
                "not vertical/square or longer than ~180 s as a Short",
                item=0,
            )
        ]

    def _thumbnail_issues(self, ctx: PublishContext, asset_id: int) -> list[Issue]:
        asset = self._thumbnail_asset(ctx, asset_id)
        if asset is None or asset.kind != "image":
            return [
                Issue(
                    "error",
                    "thumbnail_invalid",
                    "the thumbnail is not an image asset in this workspace",
                    field="thumbnail_asset_id",
                )
            ]
        if asset.size_bytes > YOUTUBE_UPLOAD.thumbnail_max_bytes:
            return [
                Issue(
                    "error",
                    "thumbnail_too_large",
                    "YouTube custom thumbnails are limited to 50 MB",
                    field="thumbnail_asset_id",
                )
            ]
        return []

    def _thumbnail_asset(self, ctx: PublishContext, asset_id: int) -> MediaAssetV2 | None:
        with self.database.session() as session:
            asset = session.get(MediaAssetV2, asset_id)
            if asset is None or asset.workspace_id != ctx.workspace_id:
                return None
            session.expunge(asset)
            return asset

    # ------------------------------------------------------------------ send

    def send(self, ctx: PublishContext, progress: Progress) -> SendResult:
        if not ctx.media:
            raise PublishRefused("media_missing", "the target has no video to upload")
        video = ctx.media[0]
        settings = self._settings(ctx)
        # Last-resort guard, before the first HTTP call: the declarations
        # preflight requires must be explicit; None must never reach YouTube
        # (an omitted flag is itself a declaration there).
        missing = [
            name
            for name, value in (
                ("privacy", settings.privacy),
                ("made_for_kids", settings.made_for_kids),
                ("has_paid_product_placement", settings.has_paid_product_placement),
            )
            if value is None
        ]
        if missing:
            raise PublishRefused(
                "settings_incomplete",
                "the YouTube declarations are incomplete: "
                + ", ".join(missing)
                + " must be set explicitly",
            )
        publish_at = self._publish_at(ctx, settings)
        token = ctx.access_token()
        self._check_channel(ctx, token)

        session_uri = str(ctx.resumable.get("session_uri") or "") or None
        offset = 0
        if session_uri:
            state, confirmed, video_id = self._session_state(ctx, token, session_uri, video)
            if state == "complete" and video_id:
                # The final PUT already finished before the process died.
                return self._processing(video_id)
            if state == "incomplete" and confirmed is not None:
                offset = confirmed
            else:
                session_uri = None
        if session_uri is None:
            session_uri = self._start_session(ctx, token, video, settings, publish_at)
            offset = 0
            self._persist(ctx, session_uri=session_uri, offset=0, size=video.size_bytes)

        stream = ctx.open_media(video)
        try:
            return self._upload(ctx, token, stream, session_uri, video, offset, progress)
        finally:
            stream.close()

    def _processing(self, video_id: str) -> SendResult:
        return SendResult(
            status="processing",
            external_id=video_id,
            permalink=_watch_url(video_id),
            detail="uploaded; YouTube is processing the video",
            next_poll_seconds=POLL_SECONDS,
            response={"video_id": video_id},
        )

    def _session_state(
        self, ctx: PublishContext, token: TokenSet, session_uri: str, video: MediaRef
    ) -> tuple[str, int | None, str | None]:
        """Ask a saved session how much arrived (spec: `Content-Range: bytes */N`)."""
        response = provider_request(
            self.client,
            "YouTube",
            "PUT",
            session_uri,
            headers={
                **self._bearer(token),
                **youtube_status_query_headers(video.size_bytes),
            },
        )
        if response.status_code == 308:
            return "incomplete", youtube_resume_offset(response.headers.get("Range")), None
        if response.status_code in (200, 201):
            try:
                data = response.json()
            except ValueError:
                data = {}
            video_id = str(_as_dict(data).get("id") or "") or None
            return "complete", None, video_id
        if response.status_code in _SESSION_EXPIRED:
            return "expired", None, None
        self._raise_for_error(response, ctx.now())
        raise AssertionError("unreachable")

    def _start_session(
        self,
        ctx: PublishContext,
        token: TokenSet,
        video: MediaRef,
        settings: YouTubeVideoSettings,
        publish_at: datetime | None,
    ) -> str:
        response = provider_request(
            self.client,
            "YouTube",
            "POST",
            f"{UPLOAD_BASE}/videos",
            params={
                "uploadType": "resumable",
                "part": INSERT_PARTS,
                "notifySubscribers": "true" if settings.notify_subscribers else "false",
            },
            json=self._metadata(ctx, settings, publish_at),
            headers={
                **self._bearer(token),
                "X-Upload-Content-Length": str(video.size_bytes),
                "X-Upload-Content-Type": video.content_type or "application/octet-stream",
                "Content-Type": "application/json; charset=UTF-8",
            },
        )
        if response.status_code != 200 or not response.headers.get("Location"):
            if response.status_code == 200:
                raise PublishError(
                    "no_session_uri",
                    "YouTube did not return an upload session URI",
                    final=True,
                    delivered=False,
                )
            self._raise_for_error(response, ctx.now())
        return str(response.headers["Location"])

    def _upload(
        self,
        ctx: PublishContext,
        token: TokenSet,
        stream: BinaryIO,
        session_uri: str,
        video: MediaRef,
        offset: int,
        progress: Progress,
    ) -> SendResult:
        size = video.size_bytes
        while offset < size:
            progressed = False
            for chunk in iter_youtube_chunks(stream, size, start_offset=offset):
                if chunk.range.is_last:
                    # Point of no return: this PUT carries the final bytes and
                    # is the request that creates the video (spec 03 section 4).
                    ctx.mark_request_sent()
                response = provider_request(
                    self.client,
                    "YouTube",
                    "PUT",
                    session_uri,
                    content=chunk.data,
                    headers={
                        **self._bearer(token),
                        "Content-Range": chunk.range.content_range,
                        "Content-Length": str(chunk.range.length),
                    },
                )
                if response.status_code == 308:
                    confirmed = youtube_resume_offset(response.headers.get("Range"))
                    self._persist(ctx, session_uri=session_uri, offset=confirmed, size=size)
                    progress.report(confirmed, size, stage="uploading")
                    if confirmed <= chunk.range.first:
                        raise PublishRetryLater(
                            "upload_stalled",
                            f"YouTube confirmed {confirmed} of {size} bytes and is not "
                            "making progress; trying later is safe",
                            retry_after=ctx.now() + SESSION_RETRY_DELAY,
                        )
                    offset = confirmed
                    progressed = True
                    break  # fresh ranges from the confirmed offset
                if response.status_code in (200, 201):
                    try:
                        data = response.json()
                    except ValueError:
                        data = {}
                    video_id = str(_as_dict(data).get("id") or "")
                    if not video_id:
                        raise PublishError(
                            "no_video_id",
                            "YouTube finished the upload without a video id",
                            delivered=None,
                        )
                    progress.report(size, size, stage="uploaded")
                    return self._processing(video_id)
                self._raise_for_error(response, ctx.now())
            if not progressed:
                break
        raise PublishRetryLater(
            "upload_incomplete",
            "the upload ended without a result; trying later is safe",
            retry_after=ctx.now() + SESSION_RETRY_DELAY,
        )

    # ------------------------------------------------------------------ poll

    def poll(self, ctx: PublishContext) -> PollResult:
        video_id = ctx.external_id or str(ctx.resumable.get("video_id") or "")
        token = ctx.access_token()
        data = self._api_get(
            token,
            f"{API_BASE}/videos",
            {"part": POLL_PARTS, "id": video_id},
        )
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return PollResult(
                status="processing",
                detail="the video is not visible to the channel yet",
                next_poll_seconds=60,
            )
        item = _as_dict(items[0])
        status = _as_dict(item.get("status"))
        upload_status = str(status.get("uploadStatus") or "")
        if upload_status == "processed":
            warnings = self._set_thumbnail(ctx, token, video_id)
            response: dict[str, Any] = {"upload_status": upload_status}
            if warnings:
                response["warnings"] = warnings
            detail = "processed"
            if warnings:
                detail += "; " + "; ".join(warnings)
            return PollResult(
                status="published",
                external_id=video_id,
                permalink=_watch_url(video_id),
                detail=detail,
                response=response,
            )
        if upload_status == "failed":
            reason = str(status.get("failureReason") or "processing_failed")
            detail = str(
                status.get("failureDescription")
                or status.get("failureReason")
                or "YouTube could not process the video"
            )
            return PollResult(
                status="failed",
                error_code=reason,
                detail=detail,
                retryable=False,
                response={"upload_status": upload_status},
            )
        if upload_status in {"rejected", "deleted"}:
            reason = str(status.get("rejectionReason") or upload_status)
            detail = str(status.get("rejectionReason") or "YouTube rejected the video")
            return PollResult(
                status="failed",
                error_code=reason,
                detail=detail,
                retryable=False,
                response={"upload_status": upload_status},
            )
        return PollResult(
            status="processing",
            detail=f"YouTube reports uploadStatus={upload_status or 'unknown'}",
            next_poll_seconds=POLL_SECONDS,
            response={"upload_status": upload_status},
        )

    def _set_thumbnail(self, ctx: PublishContext, token: TokenSet, video_id: str) -> list[str]:
        """`thumbnails.set` after processing; a 403 is a warning, never a failure."""
        raw = ctx.settings.get("thumbnail_asset_id")
        if not raw:
            return []
        asset = self._thumbnail_asset(ctx, int(raw))
        if asset is None or asset.kind != "image":
            return ["the chosen thumbnail asset was not found; it was not set"]
        if asset.size_bytes > YOUTUBE_UPLOAD.thumbnail_max_bytes:
            return ["the thumbnail is larger than YouTube's 50 MB limit; it was not set"]
        try:
            self._check_channel(ctx, token)
            media = MediaRef(
                asset_id=asset.id,
                position=0,
                kind=asset.kind,
                sha256=asset.sha256,
                size_bytes=asset.size_bytes,
                content_type=asset.content_type,
                storage=asset.storage,
                storage_key=asset.storage_key,
                original_filename=asset.original_filename,
            )
            stream = ctx.open_media(media)
            try:
                body = stream.read()
            finally:
                stream.close()
            response = provider_request(
                self.client,
                "YouTube",
                "POST",
                f"{UPLOAD_BASE}/thumbnails/set",
                params={"videoId": video_id},
                content=body,
                headers={
                    **self._bearer(token),
                    "Content-Type": asset.content_type or "image/jpeg",
                },
            )
        except Exception as exc:  # noqa: BLE001 - cosmetics must not fail the post
            return [f"the thumbnail upload failed: {safe_error_summary(exc)}"]
        if response.status_code == 403:
            return ["thumbnail not accepted for this channel"]
        if not response.is_success:
            return [f"the thumbnail upload failed (HTTP {response.status_code})"]
        return []

    # ---------------------------------------------------------------- verify

    def verify(self, ctx: PublishContext) -> VerifyResult:
        video_id = ctx.external_id
        if not video_id:
            return VerifyResult(verified=None, detail="there is no video id to read back")
        settings = self._settings(ctx)
        token = ctx.access_token()
        data = self._api_get(
            token,
            f"{API_BASE}/videos",
            {"part": VERIFY_PARTS, "id": video_id},
        )
        items = data.get("items")
        if not isinstance(items, list) or not items:
            return VerifyResult(
                verified=None,
                detail="the video is not visible to the connected channel yet",
            )
        item = _as_dict(items[0])
        snippet = _as_dict(item.get("snippet"))
        status = _as_dict(item.get("status"))
        placement = _as_dict(item.get("paidProductPlacementDetails"))
        mismatches: list[str] = []
        expected_title = settings.title or ctx.title
        if str(snippet.get("title") or "") != expected_title:
            mismatches.append(f"title is {snippet.get('title')!r}, expected {expected_title!r}")
        publish_at = self._publish_at(ctx, settings)
        if publish_at is not None:
            seen_publish_at = _parse_rfc3339(status.get("publishAt"))
            still_private = (
                status.get("privacyStatus") == "private"
                and seen_publish_at is not None
                and abs(seen_publish_at - publish_at) <= PUBLISH_AT_TOLERANCE
            )
            went_public = status.get("privacyStatus") == "public"
            if not (still_private or went_public):
                mismatches.append(
                    f"privacy is {status.get('privacyStatus')!r} with publishAt "
                    f"{status.get('publishAt')!r}, expected private until {publish_at}"
                )
        elif status.get("privacyStatus") != settings.privacy:
            mismatches.append(
                f"privacy is {status.get('privacyStatus')!r}, expected {settings.privacy!r}"
            )
        seen_placement = placement.get("hasPaidProductPlacement")
        if seen_placement is not None and bool(seen_placement) != (
            settings.has_paid_product_placement
        ):
            mismatches.append(
                f"paid product placement is {seen_placement!r}, expected "
                f"{settings.has_paid_product_placement!r}"
            )
        permalink = _watch_url(video_id)
        if mismatches:
            return VerifyResult(
                verified=False,
                permalink=permalink,
                mismatches=tuple(mismatches),
                detail="; ".join(mismatches),
            )
        return VerifyResult(
            verified=True,
            permalink=permalink,
            detail="title, privacy and declarations match what was sent",
        )

    # -------------------------------------------------------------- reconcile

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        checked: list[str] = []
        token = ctx.access_token()
        session_uri = str(ctx.resumable.get("session_uri") or "") or None
        if session_uri and ctx.media:
            checked.append("the resumable upload session")
            response = provider_request(
                self.client,
                "YouTube",
                "PUT",
                session_uri,
                headers={
                    **self._bearer(token),
                    **youtube_status_query_headers(ctx.media[0].size_bytes),
                },
            )
            if response.status_code == 308:
                return ReconcileResult(
                    status="absent",
                    detail="the upload session is incomplete; nothing was published and "
                    "the next send resumes it",
                    checked=tuple(checked),
                )
            if response.status_code in (200, 201):
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                video_id = str(_as_dict(data).get("id") or "")
                if video_id:
                    return ReconcileResult(
                        status="published",
                        external_id=video_id,
                        permalink=_watch_url(video_id),
                        detail="the upload session reports the video was created",
                        checked=tuple(checked),
                    )
            elif response.status_code not in _SESSION_EXPIRED:
                return ReconcileResult(
                    status="uncertain",
                    detail=f"the upload session answered HTTP {response.status_code}",
                    checked=tuple(checked),
                )
        # The session expired (or was never recorded): match the newest uploads
        # by title and duration inside the attempt's window (spec 08 section 2).
        checked.append(f"the channel's {RECONCILE_PAGE} newest uploads")
        matches, candidates = self._recent_matches(ctx, token)
        if len(matches) == 1 and len(candidates) == 1:
            match = matches[0]
            return ReconcileResult(
                status="published",
                external_id=match.external_id,
                permalink=match.permalink or _watch_url(match.external_id),
                detail="one recent upload matches the title, duration and time window",
                checked=tuple(checked),
            )
        if not candidates:
            return ReconcileResult(
                status="absent",
                detail="no recent upload matches; nothing was published",
                checked=tuple(checked),
            )
        return ReconcileResult(
            status="uncertain",
            detail=(
                "one recent upload matches the title but its duration cannot be "
                "verified; a person must confirm"
                if len(candidates) == 1
                else "several recent uploads could be this post; a person must choose"
            ),
            checked=tuple(checked),
            candidates=tuple(
                {
                    "video_id": item.external_id,
                    "title": item.title,
                    "permalink": item.permalink,
                    "published_at": item.published_at.isoformat() if item.published_at else None,
                }
                for item in candidates
            ),
        )

    def _recent_matches(self, ctx: PublishContext, token: TokenSet) -> tuple[list[Any], list[Any]]:
        """`(matches, candidates)` among the channel's newest uploads.

        A candidate matches the title and the attempt's time window. A match
        also agrees on duration; when either side's duration is unknown the
        item stays a title-only candidate - never enough for `published`.
        """
        settings = self._settings(ctx)
        expected_title = settings.title or ctx.title
        duration_s: float | None = None
        if ctx.media:
            raw = dict(ctx.media[0].probe).get("duration_ms")
            if isinstance(raw, int | float) and raw > 0:
                duration_s = float(raw) / 1000
        anchor = ctx.request_sent_at or ctx.planned_at or ctx.now()
        since = anchor - RECONCILE_LOOKBACK
        page = self._provider.list_content(token, cursor=None, limit=RECONCILE_PAGE)
        matches: list[Any] = []
        candidates: list[Any] = []
        for item in page.items:
            if (item.title or "") != expected_title:
                continue
            if item.published_at is not None and item.published_at < since:
                continue
            candidates.append(item)
            if (
                duration_s is not None
                and item.duration_seconds is not None
                and abs(item.duration_seconds - duration_s) <= DURATION_TOLERANCE_S
            ):
                matches.append(item)
        return matches, candidates
