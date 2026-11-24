"""The `instagram_direct` adapter (spec 08 section 3, "Send").

Two steps against `graph.instagram.com` (Instagram Login):

1. `POST /<IG_ID>/media` creates containers — up to `prepare_lead` (30 min)
   before `planned_at` via `prepare`, else inline in `send`. A carousel is
   one container per child (`is_carousel_item=true`) plus a parent
   (`media_type=CAROUSEL`, `children`). Media must sit on a public URL when
   Meta fetches it: `ctx.signed_media_url` (R2 presign) or, for
   local-stored media, a temporary R2 copy (`w/{ws}/igpub{target}p{pos}/`)
   deleted once the post reaches a terminal state.
2. `POST /<IG_ID>/media_publish` with `creation_id=<container>` — the point
   of no return, marked immediately before it. Error 9007 with subcode
   2207027 is not a refusal: read the container, take the media id when it
   says `PUBLISHED`, else retry `media_publish` with the **same**
   `creation_id` every 10 s for up to 2 minutes, then `uncertain` (a
   container can be published only once, which makes the retry safe).
   Never create a second container for a target whose first container may
   have been published; `EXPIRED` alone may be recreated once.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import MediaAssetV2
from runway.mediastore.keys import key_extension, make_media_key
from runway.mediastore.r2 import R2Store
from runway.mediastore.store import MediaStore, PartReceipt
from runway.platforms.base import (
    AuthorizationExpired,
    ProviderError,
    TokenSet,
    provider_request,
)
from runway.platforms.capabilities import Platform, equivalent_scopes
from runway.platforms.instagram import API_VERSION
from runway.platforms.limits import INSTAGRAM_DAILY_POSTS, INSTAGRAM_POLLING
from runway.platforms.post_settings import parse_settings, settings_issues
from runway.platforms.publish_capability import INSTAGRAM_PUBLISH_SCOPE
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
    RateGuard,
    ReconcileResult,
    SendResult,
    VerifyResult,
    rolling_limit,
)
from runway.posts.media import r2_config
from runway.posts.snapshot import MEDIA_RULES
from runway.posts.types import TargetPostType

logger = logging.getLogger(__name__)

POST_TYPES = frozenset(
    {
        TargetPostType.INSTAGRAM_REEL.value,
        TargetPostType.INSTAGRAM_IMAGE.value,
        TargetPostType.INSTAGRAM_CAROUSEL.value,
        TargetPostType.INSTAGRAM_STORY.value,
    }
)

# Spec 08 section 3: containers are created up to 30 minutes before planned_at.
PREPARE_LEAD = timedelta(minutes=30)
# In-send wait for a container still IN_PROGRESS before the marked publish;
# media_publish's own 9007 budget then covers two more minutes.
_STATUS_WAIT = timedelta(seconds=60)
_STATUS_STEP = timedelta(seconds=10)
# Spec: error 9007/subcode 2207027 — same creation_id every 10 s up to 2 min.
_PUBLISH_RETRY_WINDOW = timedelta(minutes=2)
_PUBLISH_RETRY_STEP = timedelta(seconds=10)
_SIGNED_TTL_SECONDS = int(INSTAGRAM_POLLING.signed_url_ttl.total_seconds())
_QUOTA_RETRY_AFTER = timedelta(hours=1)

_GRAPH = f"https://graph.instagram.com/{API_VERSION}"
_FACEBOOK_GRAPH = f"https://graph.facebook.com/{API_VERSION}"


def instagram_publish_guards() -> tuple[RateGuard, ...]:
    """50 `media_publish` calls per account per rolling 24 hours (spec 08 §3)."""
    return (
        rolling_limit(
            INSTAGRAM_DAILY_POSTS.max_count,
            INSTAGRAM_DAILY_POSTS.window,
            scope="account",
            label="posts",
        ),
    )


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _bool(value: object) -> str:
    return "true" if value in (True, "true", "1", 1) else "false"


class _MediaNotReady(Exception):
    """Meta answered 9007/2207027 — publish retried by `_publish`."""


class InstagramDirectPublisher:
    name = "instagram_direct"
    kind = "direct"
    platform = Platform.instagram
    prepare_lead = PREPARE_LEAD
    rate_guards = instagram_publish_guards()
    # `graph.instagram.com` for Instagram Login tokens (`login` extra unset);
    # `instagram_unified` overrides these two for graph.facebook.com.
    graph_base = _GRAPH
    _login = ""

    def __init__(
        self,
        settings: Settings,
        database: Database,
        *,
        client: httpx.Client | None = None,
        upload_client: httpx.Client | None = None,
        r2: MediaStore | None = None,
        clock: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
    ) -> None:
        self.settings = settings
        self.database = database
        self.client = client or httpx.Client(timeout=20.0)
        self.upload_client = upload_client or self.client
        if r2 is not None:
            self.r2: MediaStore | None = r2
        else:
            config = r2_config(settings)
            self.r2 = (
                R2Store(config, max_object_bytes=settings.media_max_upload_bytes)
                if config is not None
                else None
            )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._sleep = sleep or time.sleep

    # --------------------------------------------------------------- port

    def supports(self, post_type: str) -> bool:
        return post_type in POST_TYPES

    def preflight(self, ctx: PublishContext) -> list[Issue]:
        issues: list[Issue] = []
        kind = TargetPostType(ctx.post_type)
        try:
            parsed = parse_settings(kind, ctx.settings)
        except Exception:
            parsed = None
            issues.append(
                Issue(
                    "error",
                    "settings_invalid",
                    "the stored settings do not fit this post type; set them again",
                    field="settings",
                )
            )
        if parsed is not None:
            issues.extend(settings_issues(kind, parsed, caption=ctx.caption))
        minimum, maximum, kinds = MEDIA_RULES[kind]
        count = len(ctx.media)
        if not minimum <= count <= maximum:
            expected = str(minimum) if minimum == maximum else f"{minimum} to {maximum}"
            issues.append(
                Issue(
                    "error",
                    "media_count",
                    f"{kind.value} needs {expected} media, got {count}",
                    field="media",
                )
            )
        for media in ctx.media:
            if media.kind not in kinds:
                issues.append(
                    Issue(
                        "error",
                        "media_kind",
                        f"{kind.value} accepts {' or '.join(sorted(kinds))}; "
                        f"item {media.position + 1} is {media.kind}",
                        item=media.position,
                        field="media",
                    )
                )
            if media.kind == "image" and media.content_type not in ("image/jpeg", "image/pjpeg"):
                issues.append(
                    Issue(
                        "error",
                        "image_format",
                        "Instagram containers accept JPEG images only",
                        item=media.position,
                        field="media",
                    )
                )
        account = ctx.account
        if account is None or account.status != "connected":
            issues.append(
                Issue(
                    "error",
                    "account_required",
                    "choose a connected account",
                    field="account_id",
                )
            )
        elif INSTAGRAM_PUBLISH_SCOPE not in equivalent_scopes(account.granted_scopes):
            issues.append(
                Issue(
                    "error",
                    "scope_missing",
                    f"turn on publishing for this account to grant {INSTAGRAM_PUBLISH_SCOPE}",
                    fix="use POST /api/v1/accounts/{id}/publishing/enable",
                    field="granted_scopes",
                )
            )
        for media in ctx.media:
            if not self._media_needs_host(media):
                continue
            if self.r2 is None and ctx.signed_media_url(media, _SIGNED_TTL_SECONDS) is None:
                issues.append(
                    Issue(
                        "error",
                        "media_host",
                        "Instagram fetches media from a public URL; this "
                        "locally stored media needs R2 (RUNWAY_R2_*) configured",
                        item=media.position,
                        field="media",
                    )
                )
        return issues

    def prepare(self, ctx: PublishContext) -> Mapping[str, Any]:
        """Create every container before `planned_at`; none of this can publish."""
        token = self._token(ctx)
        ig_id = self._check_identity(ctx, token)
        state = self._new_state(ctx)
        self._create_containers(ctx, token, ig_id, state)
        for container in state["containers"]:
            status = self._container_status(token, container["id"])
            if status == "ERROR":
                raise PublishRefused(
                    "container_error", self._container_detail(token, container["id"])
                )
            if status == "PUBLISHED":
                raise PublishError(
                    "already_published",
                    "a container for this target is already published",
                    final=True,
                    delivered=False,
                )
        return {"containers": state["containers"], "r2_keys": state["r2_keys"]}

    def send(self, ctx: PublishContext, progress: Progress) -> SendResult:
        token = self._token(ctx)
        ig_id = self._check_identity(ctx, token)
        self._check_quota(token, ig_id)
        state = self._new_state(ctx, ctx.resumable)
        try:
            if not state["containers"]:
                self._create_containers(ctx, token, ig_id, state, progress)
                ctx.save_resumable(state)
            parent = self._parent(state)
            if parent is None:
                # `_create_containers` records one container per media item, so a
                # missing parent means the post carries no media at all.
                raise PublishRefused(
                    "no_container",
                    "no container was created or recorded for this target",
                )
            status = self._container_status(token, parent["id"])
            if status == "ERROR":
                raise PublishRefused(
                    "container_error", self._container_detail(token, parent["id"])
                )
            if status == "EXPIRED":
                parent = self._recreate_expired(ctx, token, ig_id, state)
                status = self._container_status(token, parent["id"])
            deadline = self._clock() + _STATUS_WAIT
            while status == "IN_PROGRESS" and self._clock() < deadline:
                self._sleep(_STATUS_STEP.total_seconds())
                status = self._container_status(token, parent["id"])
            if status == "ERROR":
                raise PublishRefused(
                    "container_error", self._container_detail(token, parent["id"])
                )
            if status == "EXPIRED":
                raise PublishRefused(
                    "container_expired",
                    "the container expired before it could publish",
                )
            # FINISHED, PUBLISHED (republished by an earlier attempt), or an
            # unreadable container all go through `media_publish`: for a PUBLISHED
            # container Meta answers 9007 and the media id is then read off the
            # container, never a second post.
            ctx.mark_request_sent()
            media_id = self._publish(token, ig_id, parent["id"])
        except PublishError as exc:
            # A terminal "nothing was published" answer (refused, revoked auth)
            # makes the staged copies useless; retryable or uncertain outcomes
            # keep them so a retry or a reconcile still has them.
            if exc.final and exc.delivered is False:
                self._cleanup_r2(ctx, state)
            raise
        self._cleanup_r2(ctx, state)
        return SendResult(
            "published",
            external_id=media_id,
            response={"container_id": parent["id"]},
        )

    def poll(self, ctx: PublishContext) -> PollResult:
        state = self._new_state(ctx, ctx.resumable)
        parent = self._parent(state)
        if parent is None:
            return PollResult("processing", detail="no container recorded yet")
        status = self._container_status(self._token(ctx), parent["id"])
        if status == "PUBLISHED":
            if ctx.account is None:
                raise PublishRefused("account_required", "choose a connected account")
            row = self._latest_media_row(self._token(ctx), ctx.account.external_account_id)
            self._cleanup_r2(ctx, state)
            return PollResult(
                "published",
                external_id=str(row["id"]) if row and row.get("id") else None,
                permalink=row.get("permalink") if row else None,
            )
        if status in ("FINISHED", "IN_PROGRESS"):
            return PollResult("processing", next_poll_seconds=60)
        self._cleanup_r2(ctx, state)
        return PollResult(
            "failed",
            error_code=f"container_{status.lower()}",
            detail=self._container_detail(self._token(ctx), parent["id"]),
            retryable=False,
        )

    def verify(self, ctx: PublishContext) -> VerifyResult:
        media_id = ctx.external_id
        if not media_id:
            return VerifyResult(verified=None, detail="no media id recorded")
        token = self._token(ctx)
        try:
            data = self._get(token, media_id, {"fields": "id,permalink,media_type,timestamp"})
        except PublishAuthRevoked:
            raise
        except PublishError as exc:
            return VerifyResult(verified=None, detail=exc.detail)
        if not data.get("id"):
            return VerifyResult(verified=None, detail="the media could not be read")
        return VerifyResult(verified=True, permalink=data.get("permalink"))

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        token = self._token(ctx)
        checked = ["the container record on the attempt"]
        state = self._new_state(ctx, ctx.resumable)
        parent = self._parent(state)
        if parent is None:
            checked.append("the account's newest media items")
            latest = self._latest_media_id(token, self._check_identity(ctx, token))
            if latest is not None:
                return ReconcileResult(
                    "uncertain",
                    detail="no container record; a recent media item may be this post",
                    checked=tuple(checked),
                    candidates=({"id": latest},),
                )
            return ReconcileResult(
                "uncertain",
                detail="no container record and nothing found to compare against",
                checked=tuple(checked),
            )
        status = self._container_status(token, parent["id"])
        if status == "PUBLISHED":
            row = self._latest_media_row(token, self._check_identity(ctx, token))
            return ReconcileResult(
                "published",
                external_id=str(row["id"]) if row and row.get("id") else None,
                permalink=row.get("permalink") if row else None,
                checked=tuple(checked),
            )
        if status in ("FINISHED", "EXPIRED", "ERROR"):
            # FINISHED never reached media_publish; EXPIRED/ERROR cannot be
            # published at all — provably absent, safe to send again.
            self._cleanup_r2(ctx, state)
            return ReconcileResult(
                "absent",
                detail=f"container is {status}; it was not published",
                checked=tuple(checked),
            )
        return ReconcileResult(
            "uncertain",
            detail=f"container is {status}; cannot tell whether it published",
            checked=tuple(checked),
        )

    # ------------------------------------------------------------- helpers

    def _new_state(
        self, ctx: PublishContext, resumable: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        state: dict[str, Any] = dict(resumable or {})
        state.setdefault("containers", [])
        state.setdefault("r2_keys", [])
        # Once a target's containers were recreated, an EXPIRED recreation is
        # final — a truthy legacy list of recreated ids counts the same.
        state.setdefault("recreated", False)
        return state

    def _parent(self, state: Mapping[str, Any]) -> dict[str, Any] | None:
        containers: list[dict[str, Any]] = state.get("containers") or []
        for container in reversed(containers):
            if container.get("role") == "parent":
                return container
        for container in containers:
            if container.get("role") == "main":
                return container
        return containers[-1] if containers else None

    def _token(self, ctx: PublishContext) -> TokenSet:
        if ctx.account is None:
            raise PublishRefused("account_required", "choose a connected account")
        try:
            token = ctx.access_token()
        except AuthorizationExpired as exc:
            raise PublishAuthRevoked(exc.detail) from exc
        # Instagram Login and Unified Login tokens are not interchangeable:
        # each only authenticates its own graph host.
        if (token.extra.get("login") == "unified") != (self._login == "unified"):
            raise PublishRefused(
                "account_wrong_adapter",
                "this account was connected through "
                + (
                    "Unified Login; target it at instagram_unified"
                    if token.extra.get("login") == "unified"
                    else "Instagram Login; target it at instagram_direct"
                ),
            )
        return token

    def _check_identity(self, ctx: PublishContext, token: TokenSet) -> str:
        """The token's Instagram user must be the target account (spec 08 §3)."""
        if ctx.account is None:
            raise PublishRefused("account_required", "choose a connected account")
        expected = str(ctx.account.external_account_id)
        held = token.extra.get("user_id")
        if held and str(held) != expected:
            raise PublishRefused(
                "account_mismatch",
                "the stored token belongs to a different Instagram account",
            )
        if not held:
            data = self._get(token, "me", {"fields": "id,user_id"})
            actual = str(data.get("user_id") or data.get("id") or "")
            if actual and actual != expected:
                raise PublishRefused(
                    "account_mismatch",
                    "the stored token belongs to a different Instagram account",
                )
        return expected

    def _check_quota(self, token: TokenSet, ig_id: str) -> None:
        """`GET /{ig}/content_publishing_limit` is the quota source of truth."""
        try:
            data = self._get(token, f"{ig_id}/content_publishing_limit", {})
        except PublishError as exc:
            logger.warning("content_publishing_limit unreadable: %s", exc.detail)
            return
        usage = _int_or_none(data.get("quota_usage"))
        config = data.get("config")
        total = _int_or_none(config.get("quota_total")) if isinstance(config, dict) else None
        if usage is not None and total is not None and usage >= total:
            raise PublishRetryLater(
                "quota_exceeded",
                f"{usage} of {total} posts in the last 24 hours",
                retry_after=self._clock() + _QUOTA_RETRY_AFTER,
            )

    # -------------------------------------------------------- container make

    def _media_url(
        self,
        ctx: PublishContext,
        media: MediaRef,
        state: dict[str, Any],
        label: str,
    ) -> str:
        url = ctx.signed_media_url(media, _SIGNED_TTL_SECONDS)
        if url is None:
            if self.r2 is None:
                raise PublishRefused(
                    "media_host",
                    "Instagram fetches media from a public URL; this locally "
                    "stored media needs R2 (RUNWAY_R2_*) configured",
                )
            key = self._stage_on_r2(ctx, media, state, label)
            url = self.r2.signed_get_url(key, _SIGNED_TTL_SECONDS, workspace_id=ctx.workspace_id)
        if url is None:
            raise PublishRefused("media_host", "no public URL could be produced for the media")
        if not str(url).isascii():
            raise PublishRefused(
                "url_not_ascii",
                "Instagram requires US-ASCII media URLs or the request fails",
            )
        return str(url)

    def _stage_on_r2(
        self, ctx: PublishContext, media: MediaRef, state: dict[str, Any], label: str
    ) -> str:
        assert self.r2 is not None
        key = make_media_key(
            ctx.workspace_id,
            f"igpub{ctx.target_id}{label}",
            media.sha256,
            key_extension(media.content_type),
        )
        if key in state["r2_keys"]:
            return key
        plan = self.r2.begin_upload(
            key,
            media.size_bytes,
            media.content_type,
            sha256=media.sha256,
            workspace_id=ctx.workspace_id,
        )
        urls = self.r2.part_upload_urls(plan)
        receipts: list[PartReceipt] = []
        with ctx.open_media(media) as handle:
            for number in plan.part_numbers():
                handle.seek(plan.part_offset(number))
                data = handle.read(plan.part_length(number))
                response = provider_request(
                    self.upload_client, "R2", "PUT", urls[number], content=data
                )
                if response.status_code >= 400:
                    raise ProviderError(
                        f"R2 part upload failed with HTTP {response.status_code}",
                        retryable=response.status_code >= 500,
                        status=response.status_code,
                    )
                receipts.append(
                    PartReceipt(
                        part_number=number,
                        size=len(data),
                        etag=str(response.headers.get("etag", "")),
                    )
                )
        self.r2.complete_upload(plan, receipts)
        state["r2_keys"].append(key)
        return key

    def _media_needs_host(self, media: MediaRef) -> bool:
        """Whether this adapter must put `media` on a public URL.

        `instagram_direct` needs one for every item; `instagram_unified`
        uploads videos to rupload.facebook.com instead.
        """
        return True

    def _cover_url(self, ctx: PublishContext, state: dict[str, Any]) -> str | None:
        cover_asset_id = ctx.settings.get("cover_asset_id")
        if not isinstance(cover_asset_id, int):
            return None
        with self.database.session() as session:
            asset = session.scalar(
                select(MediaAssetV2).where(
                    MediaAssetV2.id == cover_asset_id,
                    MediaAssetV2.workspace_id == ctx.workspace_id,
                )
            )
        if asset is None:
            raise PublishRefused(
                "cover_missing", f"cover asset {cover_asset_id} is not in this workspace"
            )
        cover = MediaRef(
            asset_id=asset.id,
            position=-1,
            kind=asset.kind,
            sha256=asset.sha256,
            size_bytes=int(asset.size_bytes),
            content_type=asset.content_type,
            storage=asset.storage,
            storage_key=asset.storage_key,
            original_filename=asset.original_filename,
        )
        return self._media_url(ctx, cover, state, "cover")

    def _container_params(
        self,
        ctx: PublishContext,
        media: MediaRef | None,
        url: str | None,
        state: dict[str, Any],
        *,
        child: bool,
    ) -> dict[str, str]:
        kind = TargetPostType(ctx.post_type)
        settings = dict(ctx.settings)
        params: dict[str, str] = {}
        if child:
            params["is_carousel_item"] = "true"
            if media is not None and media.kind == "video":
                if url:
                    params["video_url"] = url
            elif url:
                params["image_url"] = url
            return params
        if kind is TargetPostType.INSTAGRAM_STORY:
            params["media_type"] = "STORIES"
            if media is not None and media.kind == "video":
                if url:
                    params["video_url"] = url
            elif url:
                params["image_url"] = url
            return params
        if kind is TargetPostType.INSTAGRAM_CAROUSEL:
            # The parent carries caption/fields only; media_type+children are
            # set by the caller and a parent container has no media URL.
            pass
        elif kind is TargetPostType.INSTAGRAM_REEL:
            params["media_type"] = "REELS"
            if url:
                params["video_url"] = url
        elif url:
            params["image_url"] = url
        if ctx.caption:
            params["caption"] = ctx.caption
        if kind is TargetPostType.INSTAGRAM_REEL:
            if "share_to_feed" in settings:
                params["share_to_feed"] = _bool(settings["share_to_feed"])
            if settings.get("cover_asset_id") is not None:
                cover_url = self._cover_url(ctx, state)
                if cover_url is not None:
                    params["cover_url"] = cover_url
            elif settings.get("thumb_offset_ms") is not None:
                params["thumb_offset"] = str(int(settings["thumb_offset_ms"]))
            if settings.get("audio_name"):
                params["audio_name"] = str(settings["audio_name"])
            if settings.get("trial"):
                params["trial_params"] = json.dumps({"graduation_strategy": settings["trial"]})
        if kind is TargetPostType.INSTAGRAM_IMAGE:
            if settings.get("alt_text"):
                params["alt_text"] = str(settings["alt_text"])
            if settings.get("user_tags"):
                params["user_tags"] = json.dumps(list(settings["user_tags"]))
        if kind in (
            TargetPostType.INSTAGRAM_IMAGE,
            TargetPostType.INSTAGRAM_CAROUSEL,
        ) and settings.get("location_id"):
            params["location_id"] = str(settings["location_id"])
        if kind in (
            TargetPostType.INSTAGRAM_REEL,
            TargetPostType.INSTAGRAM_IMAGE,
            TargetPostType.INSTAGRAM_CAROUSEL,
        ):
            if settings.get("collaborators"):
                params["collaborators"] = json.dumps(list(settings["collaborators"]))
            if "is_ai_generated" in settings:
                params["is_ai_generated"] = _bool(settings["is_ai_generated"])
        return params

    def _media_container_params(
        self,
        ctx: PublishContext,
        media: MediaRef,
        state: dict[str, Any],
        *,
        child: bool,
    ) -> dict[str, str]:
        """The `/{ig}/media` params for one media container."""
        url = self._media_url(ctx, media, state, f"p{media.position}")
        return self._container_params(ctx, media, url, state, child=child)

    def _upload_to_container(
        self,
        ctx: PublishContext,
        token: TokenSet,
        container_id: str,
        media: MediaRef,
        progress: Progress | None,
    ) -> None:
        """Send the media bytes to a freshly created container, if the adapter
        needs to (instagram_unified's resumable video upload)."""

    def _create_containers(
        self,
        ctx: PublishContext,
        token: TokenSet,
        ig_id: str,
        state: dict[str, Any],
        progress: Progress | None = None,
    ) -> None:
        kind = TargetPostType(ctx.post_type)
        is_carousel = kind is TargetPostType.INSTAGRAM_CAROUSEL
        containers: list[dict[str, Any]] = []
        for media in ctx.media:
            params = self._media_container_params(ctx, media, state, child=is_carousel)
            data = self._post(token, f"{ig_id}/media", params)
            container_id = str(data.get("id"))
            self._upload_to_container(ctx, token, container_id, media, progress)
            containers.append(
                {
                    "id": container_id,
                    "position": media.position,
                    "role": "child" if is_carousel else "main",
                }
            )
        if is_carousel:
            params = {"media_type": "CAROUSEL"}
            params["children"] = ",".join(c["id"] for c in containers)
            params.update(self._container_params(ctx, None, "", state, child=False))
            data = self._post(token, f"{ig_id}/media", params)
            containers.append({"id": str(data.get("id")), "position": -1, "role": "parent"})
        state["containers"] = containers

    def _recreate_expired(
        self, ctx: PublishContext, token: TokenSet, ig_id: str, state: dict[str, Any]
    ) -> dict[str, Any]:
        """An EXPIRED container was never publishable — recreate it, once per target."""
        parent = self._parent(state)
        if parent is None:
            raise PublishRefused("container_expired", "no container record")
        if state.get("recreated"):
            raise PublishRefused(
                "container_expired",
                "the recreated container expired too; release again to rebuild",
            )
        state["recreated"] = True
        if parent["role"] == "parent":
            return self._recreate_carousel(ctx, token, ig_id, state)
        media = next((m for m in ctx.media if m.position == parent.get("position")), ctx.media[0])
        params = self._media_container_params(ctx, media, state, child=False)
        data = self._post(token, f"{ig_id}/media", params)
        new_id = str(data.get("id"))
        self._upload_to_container(ctx, token, new_id, media, None)
        new_main = {"id": new_id, "position": media.position, "role": "main"}
        state["containers"] = [c for c in state["containers"] if c["id"] != parent["id"]] + [
            new_main
        ]
        ctx.save_resumable(state)
        return new_main

    def _recreate_carousel(
        self, ctx: PublishContext, token: TokenSet, ig_id: str, state: dict[str, Any]
    ) -> dict[str, Any]:
        """Rebuild an expired carousel: fresh children first, then the parent.

        The children expired with the parent, so their container ids cannot be
        reused — each is recreated through the same staging and upload path a
        first-time child takes.
        """
        children: list[dict[str, Any]] = []
        for old in [c for c in state["containers"] if c["role"] != "parent"]:
            media = next(
                (m for m in ctx.media if m.position == old.get("position")),
                None,
            )
            if media is None:
                raise PublishRefused(
                    "container_expired",
                    "carousel containers expired; release again to rebuild",
                )
            params = self._media_container_params(ctx, media, state, child=True)
            data = self._post(token, f"{ig_id}/media", params)
            new_id = str(data.get("id"))
            self._upload_to_container(ctx, token, new_id, media, None)
            children.append({"id": new_id, "position": media.position, "role": "child"})
        params = {"media_type": "CAROUSEL"}
        params["children"] = ",".join(c["id"] for c in children)
        params.update(self._container_params(ctx, None, "", state, child=False))
        data = self._post(token, f"{ig_id}/media", params)
        new_parent = {"id": str(data.get("id")), "position": -1, "role": "parent"}
        state["containers"] = [*children, new_parent]
        ctx.save_resumable(state)
        return new_parent

    # --------------------------------------------------------- graph access

    def _get(self, token: TokenSet, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        response = provider_request(
            self.client,
            "Instagram",
            "GET",
            f"{self.graph_base}/{path.lstrip('/')}",
            params={k: v for k, v in params.items() if v is not None},
            headers={"Authorization": f"Bearer {token.access_token}"},
        )
        return self._result(response)

    def _post(self, token: TokenSet, path: str, params: Mapping[str, Any]) -> dict[str, Any]:
        response = provider_request(
            self.client,
            "Instagram",
            "POST",
            f"{self.graph_base}/{path.lstrip('/')}",
            data={k: v for k, v in params.items() if v is not None},
            headers={"Authorization": f"Bearer {token.access_token}"},
        )
        return self._result(response)

    @staticmethod
    def _error_message(error: object, fallback: str) -> str:
        if isinstance(error, dict):
            for key in ("error_user_msg", "message"):
                value = error.get(key)
                if isinstance(value, str) and value:
                    return value
        return fallback

    def _result(self, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        error = data.get("error")
        if isinstance(error, dict):
            code = _int_or_none(error.get("code"))
            subcode = _int_or_none(error.get("error_subcode"))
            message = self._error_message(error, "Instagram API error")
            if code == 9007 and subcode == 2207027:
                raise _MediaNotReady(message)
            if response.status_code == 401 or code == 190:
                raise PublishAuthRevoked(message)
            if code in {4, 17, 32, 613} or response.status_code == 429:
                raise PublishRetryLater("rate_limited", message)
            if code == 10 or (code is not None and 200 <= code <= 299):
                raise PublishRefused("permission_missing", message)
            raise PublishRefused(f"instagram_{code if code is not None else 'error'}", message)
        if response.status_code == 401:
            raise PublishAuthRevoked("Instagram authorization expired")
        if response.status_code == 429:
            raise PublishRetryLater("rate_limited", "Instagram rate limit exceeded")
        if response.status_code >= 500:
            raise PublishRetryLater(
                "platform_5xx", f"Instagram request failed with HTTP {response.status_code}"
            )
        if response.status_code >= 400:
            raise PublishRefused(
                "platform_error", f"Instagram request failed with HTTP {response.status_code}"
            )
        return data

    def _container_status(self, token: TokenSet, container_id: str) -> str:
        try:
            data = self._get(token, str(container_id), {"fields": "status_code,status"})
        except PublishError:
            return ""
        return str(data.get("status_code") or "")

    def _container_detail(self, token: TokenSet, container_id: str) -> str:
        try:
            data = self._get(token, str(container_id), {"fields": "status_code,status"})
        except PublishError as exc:
            return exc.detail
        status = data.get("status")
        code = data.get("status_code")
        if isinstance(status, str) and status:
            return f"container is {code}: {status}"
        return f"container is {code}"

    def _publish(self, token: TokenSet, ig_id: str, container_id: str) -> str:
        """media_publish with the spec 08 §3 9007/2207027 rule.

        Same `creation_id` every 10 s for up to two minutes; a PUBLISHED
        container yields the media id; after the window the outcome is
        unknown (the container may still publish once).
        """
        deadline = self._clock() + _PUBLISH_RETRY_WINDOW
        while True:
            try:
                data = self._post(token, f"{ig_id}/media_publish", {"creation_id": container_id})
            except _MediaNotReady:
                status = self._container_status(token, container_id)
                if status == "PUBLISHED":
                    media_id = self._published_media_id(token, ig_id, container_id)
                    if media_id is not None:
                        return media_id
                if self._clock() >= deadline:
                    raise PublishError(
                        "media_not_ready",
                        "media_publish kept answering 9007/2207027 for two "
                        "minutes; the container may still publish once",
                        delivered=None,
                    ) from None
                self._sleep(_PUBLISH_RETRY_STEP.total_seconds())
                continue
            media_id = data.get("id")
            if not media_id:
                raise PublishError(
                    "no_media_id",
                    "media_publish returned no media id",
                    delivered=None,
                )
            return str(media_id)

    def _latest_media_row(self, token: TokenSet, ig_id: str) -> dict[str, Any] | None:
        data = self._get(token, f"{ig_id}/media", {"fields": "id,permalink,timestamp", "limit": 5})
        items = data.get("data")
        if isinstance(items, list) and items and isinstance(items[0], dict):
            return items[0]
        return None

    def _latest_media_id(self, token: TokenSet, ig_id: str) -> str | None:
        row = self._latest_media_row(token, ig_id)
        if row is not None and row.get("id"):
            return str(row["id"])
        return None

    def _published_media_id(self, token: TokenSet, ig_id: str, container_id: str) -> str | None:
        """The media id of a PUBLISHED container, from the newest media items."""
        return self._latest_media_id(token, ig_id)

    def _cleanup_r2(self, ctx: PublishContext, state: dict[str, Any]) -> None:
        """Delete the temporary R2 copies once the post reached a terminal state."""
        if self.r2 is None:
            state["r2_keys"] = []
            return
        remaining: list[str] = []
        for key in state.get("r2_keys") or []:
            try:
                self.r2.delete(key, workspace_id=ctx.workspace_id)
            except Exception as exc:  # noqa: BLE001 - best-effort cleanup
                logger.warning("temporary R2 copy %s could not be deleted: %s", key, exc)
                remaining.append(key)
        state["r2_keys"] = remaining
        if ctx.attempt_id is not None:
            # Only a send context has an open attempt to persist on; poll,
            # verify and reconcile contexts refuse save_resumable — the
            # deletion itself already happened either way.
            ctx.save_resumable(state)


_RUPLOAD_BASE = "https://rupload.facebook.com/ig-api-upload"
# Bounded resumable-upload recovery: Meta answers a dropped upload with the
# byte offset it already has; resume from there at most this many times.
_MAX_RESUMES = 4


class InstagramUnifiedPublisher(InstagramDirectPublisher):
    """`instagram_unified` (spec 08 section 3, "Unified Login adapter").

    Same container/publish flow as `instagram_direct` on
    `graph.facebook.com`, except videos: the container is created with
    `upload_type=resumable` and the bytes go from disk to
    `rupload.facebook.com/ig-api-upload/<container-id>` — no public URL and
    no R2 copy. Images still need a public `image_url`.
    """

    name = "instagram_unified"
    graph_base = _FACEBOOK_GRAPH
    _login = "unified"

    def _media_needs_host(self, media: MediaRef) -> bool:
        # Videos upload from disk; only images still need a public URL.
        return media.kind != "video"

    def _media_container_params(
        self,
        ctx: PublishContext,
        media: MediaRef,
        state: dict[str, Any],
        *,
        child: bool,
    ) -> dict[str, str]:
        if media.kind == "video":
            params = self._container_params(ctx, media, None, state, child=child)
            params["upload_type"] = "resumable"
            return params
        return super()._media_container_params(ctx, media, state, child=child)

    def _upload_to_container(
        self,
        ctx: PublishContext,
        token: TokenSet,
        container_id: str,
        media: MediaRef,
        progress: Progress | None,
    ) -> None:
        if media is None or media.kind != "video":
            return
        size = media.size_bytes
        offset = 0
        resumes = 0
        with ctx.open_media(media) as handle:
            while offset < size:
                handle.seek(offset)
                body = handle.read(size - offset)
                response = provider_request(
                    self.upload_client,
                    "Instagram",
                    "POST",
                    f"{_RUPLOAD_BASE}/{container_id}",
                    headers={
                        "Authorization": f"OAuth {token.access_token}",
                        "offset": str(offset),
                        "file_size": str(size),
                    },
                    content=body,
                )
                if response.status_code < 300:
                    if progress is not None:
                        progress.report(size, size, stage="upload")
                    return
                resume_at = response.headers.get("offset")
                resumes += 1
                if resume_at is not None and resumes <= _MAX_RESUMES:
                    try:
                        resumed = int(resume_at)
                    except ValueError:
                        resumed = -1
                    if offset < resumed < size:
                        offset = resumed
                        if progress is not None:
                            progress.report(offset, size, stage="upload")
                        continue
                raise ProviderError(
                    f"the resumable upload failed with HTTP {response.status_code}",
                    retryable=response.status_code >= 500,
                    status=response.status_code,
                )
