from __future__ import annotations

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
    ) -> str:
        params = {
            "client_key": self.settings.tiktok_client_key or "",
            "scope": ",".join(TIKTOK_SCOPES),
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
        return self._token_from_response(data)

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
        if isinstance(error, dict) and error:
            code = str(error.get("code") or "")
            message = str(error.get("message") or "TikTok API error")
            log_id = error.get("log_id")
            if log_id:
                message = f"{message} (log_id {log_id})"
            if code and code != "ok":
                if code in {"access_token_invalid", "invalid_grant"}:
                    raise AuthorizationExpired(message, status=response.status_code)
                if code == "rate_limit_exceeded":
                    raise RateLimited(message, status=response.status_code)
                if code in {"scope_not_authorized", "scope_permission_missed"}:
                    raise PermissionMissing(message, status=response.status_code)
                raise ProviderError(
                    message,
                    retryable=response.status_code >= 500,
                    status=response.status_code,
                )
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
