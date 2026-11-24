from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx

from runway.config import Settings
from runway.platforms.base import (
    AccountIdentity,
    AuthorizationExpired,
    CapabilityUnsupported,
    CommentItem,
    ContentItem,
    DailyMetric,
    Page,
    PermissionMissing,
    PlatformProvider,
    ProviderError,
    RateLimited,
    ReplyResult,
    TokenSet,
    provider_request,
)
from runway.platforms.capabilities import (
    TIKTOK_SCOPES,
    CapabilityEntry,
    Platform,
    capability_matrix,
)

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
API_BASE = "https://open.tiktokapis.com/v2"


@dataclass(frozen=True, slots=True)
class CreatorInfo:
    """What `POST /v2/post/publish/creator_info/query/` returns about a creator.

    Queried fresh before every post and by the mandatory posting screen: the
    nickname shown to the person, the privacy options they may pick (never
    preselected), which interactions the creator has disabled, and the longest
    video their account may post.
    """

    creator_nickname: str | None
    creator_username: str | None
    creator_avatar_url: str | None
    privacy_level_options: list[str] = field(default_factory=list)
    comment_disabled: bool = False
    duet_disabled: bool = False
    stitch_disabled: bool = False
    max_video_post_duration_sec: int | None = None


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


class TikTokProvider(PlatformProvider):
    platform = Platform.tiktok

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=20.0)

    def capabilities(self) -> list[CapabilityEntry]:
        return capability_matrix(Platform.tiktok)

    def configured(self) -> bool:
        return self.settings.tiktok_oauth_configured

    # ------------------------------------------------------------------ OAuth

    def authorization_url(
        self,
        *,
        state: str,
        redirect_uri: str,
        code_challenge: str | None,
        scopes: Sequence[str] | None = None,
    ) -> str:
        params = {
            "client_key": self.settings.tiktok_client_key or "",
            "scope": ",".join(scopes if scopes is not None else TIKTOK_SCOPES),
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "state": state,
        }
        return f"{AUTH_URL}?{httpx.QueryParams(params)}"

    def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None,
    ) -> TokenSet:
        response = provider_request(
            self.client,
            "TikTok",
            "POST",
            f"{API_BASE}/oauth/token/",
            data={
                "client_key": self.settings.tiktok_client_key or "",
                "client_secret": self.settings.tiktok_client_secret or "",
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        data = self._json_or_raise(response)
        return self._token_from_response(data)

    def refresh(self, token: TokenSet) -> TokenSet:
        if not token.refresh_token:
            raise AuthorizationExpired("TikTok refresh token is missing", status=401)
        response = provider_request(
            self.client,
            "TikTok",
            "POST",
            f"{API_BASE}/oauth/token/",
            data={
                "client_key": self.settings.tiktok_client_key or "",
                "client_secret": self.settings.tiktok_client_secret or "",
                "code": "",
                "grant_type": "refresh_token",
                "refresh_token": token.refresh_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        data = self._json_or_raise(response)
        refreshed = self._token_from_response(data)
        # TikTok rotates the refresh token on every refresh and sends the new
        # one with its own refresh_expires_in. Fields a response omits keep the
        # values an earlier response gave; nothing falls back to a constant.
        if refreshed.refresh_token is None:
            refreshed.refresh_token = token.refresh_token
        if not isinstance(data.get("scope"), str) or not data["scope"].strip():
            # A refresh that names no scopes grants exactly what the account
            # already had — never the connect-time default list.
            refreshed.scopes = list(token.scopes)
        refreshed.extra = {**token.extra, **refreshed.extra}
        return refreshed

    def revoke(self, token: TokenSet) -> bool:
        response = provider_request(
            self.client,
            "TikTok",
            "POST",
            f"{API_BASE}/oauth/revoke/",
            data={
                "client_key": self.settings.tiktok_client_key or "",
                "client_secret": self.settings.tiktok_client_secret or "",
                "token": token.access_token,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            self._json_or_raise(response)
        except ProviderError:
            return False
        return True

    def _token_from_response(self, data: dict[str, Any]) -> TokenSet:
        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ProviderError("TikTok token response did not include access_token")
        expires_in = _int_or_none(data.get("expires_in"))
        refresh_expires = _int_or_none(data.get("refresh_expires_in"))
        scope_value = data.get("scope")
        scopes = (
            [scope.strip() for scope in scope_value.split(",") if scope.strip()]
            if isinstance(scope_value, str)
            else list(TIKTOK_SCOPES)
        )
        extra: dict[str, str] = {}
        if data.get("open_id"):
            extra["open_id"] = str(data["open_id"])
        if refresh_expires is not None:
            extra["refresh_expires_at"] = (
                datetime.now(UTC) + timedelta(seconds=refresh_expires)
            ).isoformat()
        return TokenSet(
            access_token=access_token,
            refresh_token=data.get("refresh_token")
            if isinstance(data.get("refresh_token"), str)
            else None,
            expires_at=(
                datetime.now(UTC) + timedelta(seconds=expires_in)
                if expires_in is not None
                else None
            ),
            scopes=scopes,
            extra=extra,
        )

    # -------------------------------------------------------------- HTTP core

    def _json_or_raise(self, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        error = data.get("error")
        if isinstance(error, str) and error and error != "ok":
            # The OAuth token endpoint reports errors as a top-level string
            # (RFC 6749 style), unlike the data API's error object.
            description = data.get("error_description")
            message = description if isinstance(description, str) and description else error
            log_id = data.get("log_id")
            if log_id:
                message = f"{message} (log_id {log_id})"
            if error == "invalid_grant" or response.status_code == 401:
                raise AuthorizationExpired(message, status=response.status_code)
            raise ProviderError(
                message,
                retryable=response.status_code >= 500 or response.status_code == 429,
                status=response.status_code,
            )
        if isinstance(error, dict) and error:
            code = str(error.get("code") or "")
            message = str(error.get("message") or "TikTok API error")
            log_id = error.get("log_id")
            if log_id:
                message = f"{message} (log_id {log_id})"
            if code and code != "ok":
                if code in {"access_token_invalid", "invalid_grant"}:
                    exc: ProviderError = AuthorizationExpired(message, status=response.status_code)
                elif code == "rate_limit_exceeded":
                    exc = RateLimited(message, status=response.status_code)
                elif code in {"scope_not_authorized", "scope_permission_missed"}:
                    exc = PermissionMissing(message, status=response.status_code)
                else:
                    exc = ProviderError(
                        message,
                        retryable=response.status_code >= 500,
                        status=response.status_code,
                    )
                exc.code = code
                raise exc
        if response.status_code == 401:
            raise AuthorizationExpired("TikTok authorization expired or was revoked", status=401)
        if response.status_code == 429:
            raise RateLimited("TikTok rate limit exceeded", status=429)
        if response.status_code >= 400:
            raise ProviderError(
                f"TikTok request failed with HTTP {response.status_code}",
                retryable=response.status_code >= 500,
                status=response.status_code,
            )
        return data

    def _api_get(self, token: TokenSet, path: str, params: dict[str, Any]) -> dict[str, Any]:
        response = provider_request(
            self.client,
            "TikTok",
            "GET",
            f"{API_BASE}/{path.lstrip('/')}",
            params=params,
            headers={"Authorization": f"Bearer {token.access_token}"},
        )
        return self._json_or_raise(response)

    def _api_post(
        self,
        token: TokenSet,
        path: str,
        body: dict[str, Any],
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = provider_request(
            self.client,
            "TikTok",
            "POST",
            f"{API_BASE}/{path.lstrip('/')}",
            params=params,
            json=body,
            headers={
                "Authorization": f"Bearer {token.access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
        )
        return self._json_or_raise(response)

    # ------------------------------------------------------------ publishing

    def creator_info(self, token: TokenSet) -> CreatorInfo:
        """The mandatory-screen facts about the creator, fetched fresh each call.

        Required before every publish init: the privacy options the screen may
        offer, the interactions the creator has disabled, and their maximum
        video duration.
        """
        data = self._api_post(token, "post/publish/creator_info/query/", {})
        payload = data.get("data")
        payload = payload if isinstance(payload, dict) else {}
        options = payload.get("privacy_level_options")
        return CreatorInfo(
            creator_nickname=(
                str(payload["creator_nickname"]) if payload.get("creator_nickname") else None
            ),
            creator_username=(
                str(payload["creator_username"]) if payload.get("creator_username") else None
            ),
            creator_avatar_url=(
                str(payload["creator_avatar_url"]) if payload.get("creator_avatar_url") else None
            ),
            privacy_level_options=(
                [str(option) for option in options] if isinstance(options, list) else []
            ),
            comment_disabled=bool(payload.get("comment_disabled", False)),
            duet_disabled=bool(payload.get("duet_disabled", False)),
            stitch_disabled=bool(payload.get("stitch_disabled", False)),
            max_video_post_duration_sec=_int_or_none(payload.get("max_video_post_duration_sec")),
        )

    def publish_init(
        self,
        token: TokenSet,
        *,
        inbox: bool,
        source_info: Mapping[str, Any],
        post_info: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Start a Content Posting API upload; returns `publish_id` and `upload_url`.

        `inbox` selects `post/publish/inbox/video/init/` (a draft in the creator's
        inbox); otherwise the Direct Post endpoint `post/publish/video/init/` is
        used and `post_info` carries the mandatory-screen choices. This is the
        request that can create the post, so callers mark `request_sent_at`
        first. The upload URL lives for one hour.
        """
        body: dict[str, Any] = {"source_info": dict(source_info)}
        if post_info is not None:
            body["post_info"] = dict(post_info)
        path = "post/publish/inbox/video/init/" if inbox else "post/publish/video/init/"
        data = self._api_post(token, path, body)
        payload = data.get("data")
        payload = payload if isinstance(payload, dict) else {}
        publish_id = payload.get("publish_id")
        if not isinstance(publish_id, str) or not publish_id:
            raise ProviderError("TikTok init returned no publish_id")
        upload_url = payload.get("upload_url")
        if not isinstance(upload_url, str) or not upload_url:
            raise ProviderError("TikTok init returned no upload_url")
        return {"publish_id": publish_id, "upload_url": upload_url}

    def upload_chunk(
        self,
        upload_url: str,
        *,
        data: bytes,
        content_range: str,
        content_type: str,
    ) -> int:
        """PUT one chunk to the session's upload URL; returns the HTTP status.

        The upload URL is signed, so no Authorization header goes on it
        (research/tiktok.md §5). Expected answers: 206 continue, 201 done,
        403 URL expired, 416 range mismatch. Anything else raises.
        """
        response = provider_request(
            self.client,
            "TikTok",
            "PUT",
            upload_url,
            content=data,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(data)),
                "Content-Range": content_range,
            },
        )
        if response.status_code in (201, 206, 403, 416):
            return response.status_code
        if response.status_code == 429:
            raise RateLimited("TikTok upload rate limit exceeded", status=response.status_code)
        raise ProviderError(
            f"TikTok chunk upload failed with HTTP {response.status_code}",
            retryable=response.status_code >= 500,
            status=response.status_code,
        )

    def publish_status(self, token: TokenSet, publish_id: str) -> dict[str, Any]:
        """`POST /v2/post/publish/status/fetch/` for one publish id."""
        data = self._api_post(token, "post/publish/status/fetch/", {"publish_id": publish_id})
        payload = data.get("data")
        return payload if isinstance(payload, dict) else {}

    # --------------------------------------------------------------- identity

    def fetch_identity(self, token: TokenSet) -> AccountIdentity:
        data = self._api_get(
            token,
            "user/info/",
            {
                "fields": (
                    "open_id,union_id,avatar_url,display_name,username,"
                    "profile_deep_link,follower_count,following_count,"
                    "likes_count,video_count"
                )
            },
        )
        user = data.get("data")
        user = user.get("user") if isinstance(user, dict) else None
        if not isinstance(user, dict):
            raise ProviderError("TikTok user/info returned no user object")
        username = user.get("username")
        return AccountIdentity(
            external_account_id=str(user.get("open_id") or ""),
            handle=username if isinstance(username, str) else None,
            display_name=(str(user["display_name"]) if user.get("display_name") else None),
            avatar_url=str(user["avatar_url"]) if user.get("avatar_url") else None,
            profile_url=(str(user["profile_deep_link"]) if user.get("profile_deep_link") else None),
            account_type="creator",
            followers=_int_or_none(user.get("follower_count")),
            following=_int_or_none(user.get("following_count")),
            content_count=_int_or_none(user.get("video_count")),
            total_likes=_int_or_none(user.get("likes_count")),
            raw=user,
        )

    # --------------------------------------------------------------- content

    def list_content(
        self,
        token: TokenSet,
        *,
        cursor: dict[str, Any] | None,
        limit: int,
    ) -> Page[ContentItem]:
        body: dict[str, Any] = {"max_count": min(max(limit, 1), 20)}
        if cursor and cursor.get("cursor") is not None:
            body["cursor"] = cursor["cursor"]
        response = provider_request(
            self.client,
            "TikTok",
            "POST",
            f"{API_BASE}/video/list/",
            params={
                "fields": (
                    "id,title,video_description,duration,cover_image_url,"
                    "embed_link,share_url,create_time,like_count,comment_count,"
                    "share_count,view_count"
                )
            },
            json=body,
            headers={
                "Authorization": f"Bearer {token.access_token}",
                "Content-Type": "application/json",
            },
        )
        data = self._json_or_raise(response)
        payload = data.get("data")
        payload = payload if isinstance(payload, dict) else {}
        items: list[ContentItem] = []
        videos = payload.get("videos")
        for video in videos if isinstance(videos, list) else []:
            if not isinstance(video, dict):
                continue
            create_time = _int_or_none(video.get("create_time"))
            items.append(
                ContentItem(
                    external_id=str(video.get("id") or ""),
                    content_type="video",
                    title=str(video["title"]) if video.get("title") else None,
                    caption=(
                        str(video["video_description"]) if video.get("video_description") else None
                    ),
                    permalink=(str(video["share_url"]) if video.get("share_url") else None),
                    thumbnail_url=(
                        str(video["cover_image_url"]) if video.get("cover_image_url") else None
                    ),
                    published_at=(
                        datetime.fromtimestamp(create_time, tz=UTC)
                        if create_time is not None
                        else None
                    ),
                    duration_seconds=_int_or_none(video.get("duration")),
                    metrics={
                        "views": _int_or_none(video.get("view_count")),
                        "likes": _int_or_none(video.get("like_count")),
                        "comments": _int_or_none(video.get("comment_count")),
                        "shares": _int_or_none(video.get("share_count")),
                    },
                    raw=video,
                )
            )
        next_cursor: dict[str, Any] | None = None
        if payload.get("has_more") and payload.get("cursor") is not None:
            next_cursor = {"cursor": payload["cursor"]}
        return Page(items=items, next_cursor=next_cursor)

    def fetch_content_metrics(
        self,
        token: TokenSet,
        external_ids: list[str],
    ) -> dict[str, dict[str, int | float | None]]:
        return {}

    def fetch_account_daily_metrics(
        self,
        token: TokenSet,
        *,
        since: date,
        until: date,
    ) -> list[DailyMetric]:
        return []

    def list_comments(
        self,
        token: TokenSet,
        *,
        content_external_id: str,
        cursor: dict[str, Any] | None,
        limit: int,
    ) -> Page[CommentItem]:
        raise CapabilityUnsupported(
            "TikTok comments require the Business Account API, which is not implemented"
        )

    def reply_to_comment(
        self,
        token: TokenSet,
        *,
        comment_external_id: str,
        text: str,
    ) -> ReplyResult:
        raise CapabilityUnsupported("TikTok comment replies require the Business Account API")
