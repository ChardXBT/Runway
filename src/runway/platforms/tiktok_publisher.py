"""The live TikTok publishers: `tiktok_inbox` and `tiktok_direct` (spec 08 section 4).

Both share the same mechanics: `request_sent_at` is committed, the init call
hands out a `publish_id` and an upload URL that lives for one hour
(`TIKTOK_CHUNKS.upload_url_ttl`), the chunks `PUT` in order with
`Content-Range` (206 continue, 201 done, 403/416 restart from init), and
`status/fetch` walks the publish id to its outcome.

- `TikTokInboxPublisher` (`tiktok_draft`, scope `video.upload`): the video lands
  in the creator's TikTok inbox; the target sits in `needs_user` until the
  creator finishes and posts it in the app.
- `TikTokDirectPublisher` (`tiktok_video`, scope `video.publish`): posted by the
  API. It must never run below `tiktok_app_status == "audited"` (spec 08 section
  4): the capability layer refuses the choice, and preflight refuses again in
  case the app status changed between release and send.
"""

from __future__ import annotations

import threading
from collections import deque
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from runway.config import Settings
from runway.db.models import utcnow
from runway.mediastore.delivery import iter_tiktok_chunks, plan_tiktok_chunks
from runway.mediastore.probe import ProbeResult
from runway.mediastore.validation import validate
from runway.platforms.base import (
    AuthorizationExpired,
    ProviderError,
    RateLimited,
    TokenSet,
)
from runway.platforms.capabilities import Platform
from runway.platforms.limits import TIKTOK_CHUNKS, TIKTOK_INIT_RATE
from runway.platforms.publisher import (
    AdapterKind,
    Issue,
    MediaRef,
    PollResult,
    Progress,
    PublishAuthRevoked,
    PublishContext,
    PublishRefused,
    PublishRetryLater,
    ReconcileResult,
    SendResult,
    VerifyResult,
    pending_user_limit,
    rolling_limit,
)
from runway.platforms.tiktok import TikTokProvider
from runway.posts.types import TargetPostType

_PROCESSING_STATUSES = frozenset({"PROCESSING_UPLOAD", "PROCESSING_DOWNLOAD"})
# Spec 08 section 4: only these `fail_reason` values may be retried; every other
# reason (and an unknown one) is final.
_RETRYABLE_REASONS = frozenset({"internal", "video_pull_failed"})

_REASON_DETAILS: dict[str, str] = {
    "file_format_check_failed": "TikTok rejected the file's format; convert it to MP4",
    "duration_check_failed": "the video is longer than this account may post",
    "frame_rate_check_failed": "the frame rate is outside what TikTok accepts (23-60 fps)",
    "picture_size_check_failed": (
        "the picture size is outside what TikTok accepts (each side 360-4096 px)"
    ),
    "spam_risk": "TikTok flagged the post as spam",
    "spam_risk_too_many_posts": "this creator reached the daily posting cap",
    "spam_risk_too_many_pending_share": "five drafts are already waiting in the inbox",
    "spam_risk_user_banned_from_posting": "TikTok has banned this account from posting",
    "spam_risk_text": "TikTok flagged the caption text as spam",
    "auth_removed": "the creator disconnected the app; reconnect the TikTok account",
    "publish_cancelled": "the publish was cancelled",
    "photo_pull_failed": "TikTok could not fetch the media",
    "video_pull_failed": "TikTok could not process the video; trying again may work",
    "internal": "TikTok reported an internal error; trying again may work",
}

# Init answers that mean "this post cannot go out" (research/tiktok.md section
# 4): spam risk, unaudited caps and invalid choices. They are final.
_INIT_FINAL_CODES = frozenset(
    {
        "spam_risk_too_many_posts",
        "spam_risk_too_many_pending_share",
        "spam_risk_user_banned_from_posting",
        "reached_active_user_cap",
        "unaudited_client_can_only_post_to_private_accounts",
        "privacy_level_option_mismatch",
        "url_ownership_unverified",
    }
)

_POLL_SECONDS = 30
_NEEDS_USER_POLL_SECONDS = 60
# A dead upload URL restarts from init, which costs one of the 6 init calls a
# minute a token gets (TIKTOK_INIT_RATE). Past a few restarts inside one send
# the platform is misbehaving, not the file: stop and let the job retry.
_MAX_UPLOAD_RESTARTS = 3


def _as_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _reason_detail(reason: str) -> str:
    return _REASON_DETAILS.get(reason, f"TikTok reported: {reason}")


class _TikTokPublisherBase:
    """Shared FILE_UPLOAD mechanics for the two TikTok adapters."""

    platform = Platform.tiktok
    inbox = False
    kind: AdapterKind = "direct"
    required_scope = "video.publish"

    def __init__(
        self,
        settings: Settings,
        *,
        provider: TikTokProvider,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.settings = settings
        self.provider = provider
        self.clock = clock
        # TIKTOK_INIT_RATE: init calls per account inside the sliding minute.
        self._init_calls: dict[int, deque[datetime]] = {}
        self._init_lock = threading.Lock()

    # ------------------------------------------------------------- preflight

    def _scope_issues(self, ctx: PublishContext) -> list[Issue]:
        scopes = ctx.account.granted_scopes if ctx.account is not None else ()
        if self.required_scope in scopes:
            return []
        return [
            Issue(
                "error",
                "missing_scope",
                f"the TikTok account must grant the {self.required_scope} scope; "
                "reconnect it with the publishing permissions",
            )
        ]

    def _media_issues(
        self, ctx: PublishContext, *, creator_max_duration_s: int | None = None
    ) -> list[Issue]:
        if len(ctx.media) != 1 or ctx.media[0].kind != "video":
            return [
                Issue(
                    "error",
                    "media_count",
                    f"a TikTok post is exactly one video; this target has "
                    f"{len(ctx.media)} media items",
                )
            ]
        probe_dict = dict(ctx.media[0].probe or {})
        if not probe_dict:
            return [
                Issue(
                    "error",
                    "probe_missing",
                    "the video has not been probed; media checks are not possible",
                )
            ]
        probe = ProbeResult.from_dict(probe_dict)
        found = validate(
            probe,
            Platform.tiktok,
            "tiktok_video",
            creator_max_duration_s=creator_max_duration_s,
        )
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

    # ------------------------------------------------------------------ send

    def _resumed_state(self, ctx: PublishContext) -> dict[str, Any] | None:
        """A saved upload session that is still inside its one-hour TTL."""
        state = ctx.resumable
        if not state or state.get("payload_hash") != ctx.payload_hash:
            return None
        publish_id = state.get("publish_id")
        upload_url = state.get("upload_url")
        expires_at = _as_datetime(state.get("expires_at"))
        if (
            not isinstance(publish_id, str)
            or not publish_id
            or not isinstance(upload_url, str)
            or not upload_url
            or expires_at is None
            or expires_at <= ctx.now()
        ):
            return None
        return {
            "payload_hash": ctx.payload_hash,
            "video_size": int(state.get("video_size") or 0),
            "publish_id": publish_id,
            "upload_url": upload_url,
            "expires_at": state["expires_at"],
            "next_chunk": int(state.get("next_chunk") or 0),
        }

    def _post_info(self, ctx: PublishContext) -> Mapping[str, Any] | None:
        return None

    def _init_rate(self, ctx: PublishContext) -> None:
        """TIKTOK_INIT_RATE: refuse a seventh init inside the account's minute.

        TikTok counts init calls per access token on a sliding minute; the
        in-process window refuses the call locally (before any request) so the
        count is never spent. `retry_after` is when the oldest call leaves the
        window. This guard is per process: the durable counters (rate guards,
        attempt ledger) still apply across restarts.
        """
        key = ctx.account.id if ctx.account is not None else 0
        now = ctx.now()
        with self._init_lock:
            calls = self._init_calls.setdefault(key, deque())
            while calls and calls[0] <= now - TIKTOK_INIT_RATE.window:
                calls.popleft()
            if len(calls) >= TIKTOK_INIT_RATE.max_count:
                raise PublishRetryLater(
                    "tiktok_init_rate",
                    f"TikTok allows {TIKTOK_INIT_RATE.max_count} publish inits a "
                    "minute; the send retries when the window frees",
                    retry_after=calls[0] + TIKTOK_INIT_RATE.window,
                )
            calls.append(now)

    def _init(self, ctx: PublishContext, token: TokenSet, media: MediaRef) -> dict[str, Any]:
        """Fresh publish session: init, then the resumable that survives a crash."""
        self._init_rate(ctx)
        plan = plan_tiktok_chunks(media.size_bytes)
        try:
            init = self.provider.publish_init(
                token,
                inbox=self.inbox,
                source_info=plan.source_info(),
                post_info=self._post_info(ctx),
            )
        except ProviderError as exc:
            if isinstance(exc, AuthorizationExpired):
                raise PublishAuthRevoked(
                    "the TikTok account must be reconnected before posting"
                ) from exc
            if exc.code in _INIT_FINAL_CODES:
                raise PublishRefused(str(exc.code), exc.detail) from exc
            if isinstance(exc, RateLimited):
                raise PublishRetryLater("rate_limited", exc.detail) from exc
            raise
        state = {
            "payload_hash": ctx.payload_hash,
            "video_size": media.size_bytes,
            "publish_id": init["publish_id"],
            "upload_url": init["upload_url"],
            "expires_at": (ctx.now() + TIKTOK_CHUNKS.upload_url_ttl).isoformat(),
            "next_chunk": 0,
        }
        ctx.save_resumable(state)
        return state

    def _upload(
        self,
        ctx: PublishContext,
        token: TokenSet,
        media: MediaRef,
        state: dict[str, Any],
        progress: Progress,
    ) -> dict[str, Any]:
        """PUT the remaining chunks; returns the session state that finished.

        A 403 (URL expired) or 416 (range mismatch) restarts from a fresh init,
        so the returned session may differ from the one passed in.
        """
        video_size = int(state["video_size"]) or media.size_bytes
        plan = plan_tiktok_chunks(video_size)
        stream = ctx.open_media(media)
        progress.report(0, video_size, stage="uploading")
        restarts = 0
        while True:
            dead_url = False
            for index, chunk in enumerate(iter_tiktok_chunks(stream, plan)):
                if index < int(state["next_chunk"]):
                    continue
                status = self.provider.upload_chunk(
                    str(state["upload_url"]),
                    data=chunk.data,
                    content_range=chunk.range.content_range,
                    content_type=media.content_type or "video/mp4",
                )
                if status in (403, 416):
                    # URL expired (403) or the server disagrees on progress
                    # (416): a fresh init is the documented recovery.
                    dead_url = True
                    break
                state["next_chunk"] = index + 1
                ctx.save_resumable(state)
                progress.report(chunk.range.last + 1, video_size, stage="uploading")
            if not dead_url:
                return state
            restarts += 1
            if restarts > _MAX_UPLOAD_RESTARTS:
                raise PublishRetryLater(
                    "upload_url_expired",
                    "the upload URL kept expiring before the file finished; "
                    "the send job retries later",
                )
            state = self._init(ctx, token, media)  # a fresh hour and upload URL

    def send(self, ctx: PublishContext, progress: Progress) -> SendResult:
        media = ctx.media[0]
        token = ctx.access_token()
        state = self._resumed_state(ctx)
        if state is None:
            # Init is the point of no return for both paths: the marker commits
            # before it, so a crash afterwards is reconciled, never re-sent.
            ctx.mark_request_sent()
            state = self._init(ctx, token, media)
        state = self._upload(ctx, token, media, state, progress)
        return SendResult(
            status="processing",
            external_id=str(state["publish_id"]),
            detail="the video is uploaded; TikTok is processing it",
            next_poll_seconds=_POLL_SECONDS,
            response={"publish_id": state["publish_id"], "inbox": self.inbox},
        )

    # --------------------------------------------------------------- status

    def _publish_id(self, ctx: PublishContext) -> str | None:
        publish_id = ctx.resumable.get("publish_id")
        if isinstance(publish_id, str) and publish_id:
            return publish_id
        return ctx.external_id if ctx.external_id else None

    def _permalink(self, ctx: PublishContext, post_id: str) -> str | None:
        handle = (ctx.account.handle if ctx.account else None) or ""
        handle = handle.strip().lstrip("@")
        if not handle:
            return None
        return f"https://www.tiktok.com/@{handle}/video/{post_id}"

    def _fetch_status(self, ctx: PublishContext, publish_id: str) -> dict[str, Any]:
        try:
            return self.provider.publish_status(ctx.access_token(), publish_id)
        except ProviderError as exc:
            if isinstance(exc, AuthorizationExpired):
                raise PublishAuthRevoked("the TikTok account must be reconnected") from exc
            raise

    def _published(
        self, ctx: PublishContext, data: Mapping[str, Any], publish_id: str
    ) -> tuple[str | None, str | None]:
        """(external_id, permalink) for a PUBLISH_COMPLETE status."""
        post_id = data.get("publicaly_available_post_id")  # TikTok's spelling
        if isinstance(post_id, str) and post_id:
            return post_id, self._permalink(ctx, post_id)
        return publish_id, None

    def poll(self, ctx: PublishContext) -> PollResult:
        publish_id = self._publish_id(ctx)
        if publish_id is None:
            return PollResult(
                status="processing",
                detail="waiting for the publish id to be recorded",
                next_poll_seconds=_POLL_SECONDS,
            )
        data = self._fetch_status(ctx, publish_id)
        status = str(data.get("status") or "")
        if status == "PUBLISH_COMPLETE":
            external_id, permalink = self._published(ctx, data, publish_id)
            return PollResult(
                status="published",
                external_id=external_id,
                permalink=permalink,
                response={"status": status},
            )
        if status == "SEND_TO_USER_INBOX":
            return PollResult(
                status="needs_user",
                detail="the draft is in the creator's TikTok inbox; they finish "
                "and post it in the app",
                next_poll_seconds=_NEEDS_USER_POLL_SECONDS,
                response={"status": status},
            )
        if status == "FAILED":
            reason = str(data.get("fail_reason") or "unknown")
            return PollResult(
                status="failed",
                error_code=reason,
                detail=_reason_detail(reason),
                retryable=reason in _RETRYABLE_REASONS,
                response={"status": status},
            )
        return PollResult(
            status="processing",
            detail=f"TikTok reports {status or 'no status'}",
            next_poll_seconds=_POLL_SECONDS,
            response={"status": status},
        )

    def verify(self, ctx: PublishContext) -> VerifyResult:
        publish_id = self._publish_id(ctx)
        if publish_id is None:
            return VerifyResult(verified=None, detail="no publish id was recorded")
        data = self._fetch_status(ctx, publish_id)
        status = str(data.get("status") or "")
        if status == "PUBLISH_COMPLETE":
            post_id = data.get("publicaly_available_post_id")
            if isinstance(post_id, str) and post_id:
                return VerifyResult(
                    verified=True,
                    permalink=self._permalink(ctx, post_id),
                    detail="TikTok reports the post publicly available",
                    response={"status": status},
                )
            return VerifyResult(
                verified=None,
                detail="published; not yet publicly visible",
                response={"status": status},
            )
        if status == "FAILED":
            reason = str(data.get("fail_reason") or "unknown")
            return VerifyResult(
                verified=False,
                detail=_reason_detail(reason),
                mismatches=("the platform reports the publish failed",),
                response={"status": status},
            )
        return VerifyResult(
            verified=None,
            detail=f"TikTok still reports {status or 'no status'}",
            response={"status": status},
        )

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        checked: tuple[str, ...] = ("the release marker and the saved publish id",)
        if ctx.request_sent_at is None:
            return ReconcileResult(
                status="absent",
                checked=checked,
                detail="the send marker was never committed; nothing was sent",
            )
        publish_id = self._publish_id(ctx)
        if publish_id is None:
            return ReconcileResult(
                status="absent",
                checked=checked,
                detail="init never returned a publish id, so no post can exist",
            )
        checked = (f"TikTok status/fetch for publish id {publish_id}",)
        data = self._fetch_status(ctx, publish_id)
        status = str(data.get("status") or "")
        if status == "PUBLISH_COMPLETE":
            external_id, permalink = self._published(ctx, data, publish_id)
            return ReconcileResult(
                status="published",
                external_id=external_id,
                permalink=permalink,
                checked=checked,
                response={"status": status},
            )
        if status in _PROCESSING_STATUSES:
            return ReconcileResult(
                status="processing",
                detail=f"TikTok still reports {status}",
                checked=checked,
                response={"status": status},
            )
        if status == "SEND_TO_USER_INBOX":
            return ReconcileResult(
                status="needs_user",
                detail="the draft is waiting in the creator's TikTok inbox",
                checked=checked,
                response={"status": status},
            )
        if status == "FAILED":
            reason = str(data.get("fail_reason") or "unknown")
            return ReconcileResult(
                status="absent",
                detail=_reason_detail(reason),
                checked=checked,
                response={"status": status, "fail_reason": reason},
            )
        return ReconcileResult(
            status="uncertain",
            detail=f"TikTok reports an unexpected status: {status or 'none'}",
            checked=checked,
            response={"status": status},
        )


class TikTokInboxPublisher(_TikTokPublisherBase):
    """`tiktok_draft`: FILE_UPLOAD into the creator's inbox (scope `video.upload`)."""

    name = "tiktok_inbox"
    inbox = True
    kind: AdapterKind = "draft"
    required_scope = "video.upload"

    def __init__(
        self,
        settings: Settings,
        *,
        provider: TikTokProvider | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        super().__init__(settings, provider=provider or TikTokProvider(settings), clock=clock)
        # At most 5 pending shares per creator per 24 h (TIKTOK_DAILY_INBOX_SHARES).
        self.rate_guards = (pending_user_limit(5),)

    def supports(self, post_type: str) -> bool:
        return post_type == TargetPostType.TIKTOK_DRAFT.value

    def preflight(self, ctx: PublishContext) -> list[Issue]:
        return self._scope_issues(ctx) + self._media_issues(ctx)


class TikTokDirectPublisher(_TikTokPublisherBase):
    """`tiktok_video`: Direct Post (scope `video.publish`), only when audited."""

    name = "tiktok_direct"
    kind: AdapterKind = "direct"
    required_scope = "video.publish"

    def __init__(
        self,
        settings: Settings,
        *,
        provider: TikTokProvider | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        super().__init__(settings, provider=provider or TikTokProvider(settings), clock=clock)
        # About 15 direct posts per creator per 24 h (TIKTOK_DAILY_DIRECT_POSTS).
        self.rate_guards = (rolling_limit(15, timedelta(hours=24), label="posts"),)

    def supports(self, post_type: str) -> bool:
        return post_type == TargetPostType.TIKTOK_VIDEO.value

    def _post_info(self, ctx: PublishContext) -> Mapping[str, Any]:
        settings = ctx.settings
        post_info: dict[str, Any] = {
            "title": ctx.caption,
            "privacy_level": str(settings.get("privacy_level") or ""),
            "disable_comment": not bool(settings.get("allow_comment")),
            "disable_duet": not bool(settings.get("allow_duet")),
            "disable_stitch": not bool(settings.get("allow_stitch")),
            "is_aigc": bool(settings.get("is_aigc", ctx.ai_generated)),
        }
        cover = settings.get("cover_timestamp_ms")
        if cover is not None:
            post_info["video_cover_timestamp_ms"] = int(cover)
        if settings.get("commercial_content"):
            post_info["brand_content_toggle"] = bool(settings.get("branded_content"))
            post_info["brand_organic_toggle"] = bool(settings.get("your_brand"))
        return post_info

    def preflight(self, ctx: PublishContext) -> list[Issue]:
        issues = self._scope_issues(ctx)
        media = ctx.media[0] if len(ctx.media) == 1 else None
        if media is None or media.kind != "video" or not media.probe:
            # Without a probed video there is nothing the screen checks can add.
            issues.extend(self._media_issues(ctx))
            return issues
        # Spec 08 section 4: never below audited, and refuse before any call.
        if self.settings.tiktok_app_status != "audited":
            issues.append(
                Issue(
                    "error",
                    "app_not_audited",
                    f"TikTok direct posting needs an audited app (now "
                    f"{self.settings.tiktok_app_status}); use the inbox draft instead",
                )
            )
            return issues
        info = self.provider.creator_info(ctx.access_token())
        issues.extend(
            self._media_issues(ctx, creator_max_duration_s=info.max_video_post_duration_sec)
        )
        privacy = ctx.settings.get("privacy_level")
        if not privacy:
            issues.append(
                Issue(
                    "error",
                    "privacy_required",
                    "choose who can see the video; TikTok requires the creator to pick it",
                    field="privacy_level",
                )
            )
        elif not info.privacy_level_options or str(privacy) not in info.privacy_level_options:
            issues.append(
                Issue(
                    "error",
                    "privacy_not_offered",
                    f"{privacy} is not one of this creator's privacy options; "
                    "open the posting screen and choose again",
                    field="privacy_level",
                )
            )
        disabled = {
            "allow_comment": info.comment_disabled,
            "allow_duet": info.duet_disabled,
            "allow_stitch": info.stitch_disabled,
        }
        asked = [name for name, off in disabled.items() if off and ctx.settings.get(name)]
        if asked:
            issues.append(
                Issue(
                    "error",
                    "interaction_unavailable",
                    "the creator has disabled " + ", ".join(asked).replace("allow_", ""),
                    field=asked[0],
                )
            )
        return issues
