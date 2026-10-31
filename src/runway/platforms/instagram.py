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
    INSTAGRAM_SCOPES,
    CapabilityEntry,
    Platform,
    capability_matrix,
)

API_VERSION = "v25.0"
AUTH_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"
GRAPH_BASE = f"https://graph.instagram.com/{API_VERSION}"
LONG_LIVED_URL = "https://graph.instagram.com/access_token"
REFRESH_URL = "https://graph.instagram.com/refresh_access_token"

_CONTENT_TYPES = {
    "REELS": "reel",
    "STORY": "story",
    "CAROUSEL_ALBUM": "carousel",
    "IMAGE": "image",
    "VIDEO": "video",
}

_CONTENT_METRICS = "comments,likes,reach,saved,shares,total_interactions,views"
_STORY_METRICS = "reach,replies,shares,total_interactions,views"
_DAILY_METRICS = "reach,follower_count"
_TOTAL_VALUE_METRICS = "views,profile_views,accounts_engaged,total_interactions"


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


class InstagramProvider(PlatformProvider):
    platform = Platform.instagram

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=20.0)
        self.warnings: list[str] = []

    def capabilities(self) -> list[CapabilityEntry]:
        return capability_matrix(Platform.instagram)

    def configured(self) -> bool:
        return self.settings.instagram_oauth_configured

    # ------------------------------------------------------------------ OAuth

    def authorization_url(
        self,
        *,
        state: str,
        redirect_uri: str,
        code_challenge: str | None,
    ) -> str:
        params = {
            "client_id": self.settings.instagram_app_id or "",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": ",".join(INSTAGRAM_SCOPES),
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
            "Instagram",
            "POST",
            TOKEN_URL,
            data={
                "client_id": self.settings.instagram_app_id or "",
                "client_secret": self.settings.instagram_app_secret or "",
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
                "code": code[:-2] if code.endswith("#_") else code,
            },
        )
        data = self._json_or_raise(response)
        token = self._short_token_from_response(data)
        return self.exchange_long_lived(token)

    def _short_token_from_response(self, data: dict[str, Any]) -> TokenSet:
        payload: dict[str, Any] = data
        wrapped = data.get("data")
        if isinstance(wrapped, list) and wrapped:
            first = wrapped[0]
            if isinstance(first, dict):
                payload = first
        access_token = payload.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ProviderError("Instagram token response did not include access_token")
        extra: dict[str, str] = {}
        user_id = payload.get("user_id")
        if user_id is not None:
            extra["user_id"] = str(user_id)
        permissions = payload.get("permissions")
        scopes = (
            [str(item) for item in permissions]
            if isinstance(permissions, list)
            else list(INSTAGRAM_SCOPES)
        )
        return TokenSet(
            access_token=access_token,
            scopes=scopes,
            extra=extra,
        )

    def exchange_long_lived(self, token: TokenSet) -> TokenSet:
        # Meta documents grant_type/client_secret/access_token as required query
        # params for this grant endpoint; it does not accept a Bearer header.
        response = provider_request(
            self.client,
            "Instagram",
            "GET",
            LONG_LIVED_URL,
            params={
                "grant_type": "ig_exchange_token",
                "client_secret": self.settings.instagram_app_secret or "",
                "access_token": token.access_token,
            },
        )
        data = self._json_or_raise(response)
        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ProviderError("Instagram long-lived exchange returned no token")
        expires_in = _int_or_none(data.get("expires_in"))
        return token.model_copy(
            update={
                "access_token": access_token,
                "expires_at": (
                    datetime.now(UTC) + timedelta(seconds=expires_in)
                    if expires_in is not None
                    else None
                ),
            }
        )

    def refresh(self, token: TokenSet) -> TokenSet:
        # ig_refresh_token accepts Authorization: Bearer, so the held token stays
        # out of the request URL; grant_type remains a documented query param.
        response = provider_request(
            self.client,
            "Instagram",
            "GET",
            REFRESH_URL,
            params={"grant_type": "ig_refresh_token"},
            headers={"Authorization": f"Bearer {token.access_token}"},
        )
        data = self._json_or_raise(response)
        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise AuthorizationExpired("Instagram refresh returned no token")
        expires_in = _int_or_none(data.get("expires_in"))
        return token.model_copy(
            update={
                "access_token": access_token,
                "expires_at": (
                    datetime.now(UTC) + timedelta(seconds=expires_in)
                    if expires_in is not None
                    else None
                ),
            }
        )

    def revoke(self, token: TokenSet) -> bool:
        return False

    # -------------------------------------------------------------- HTTP core

    def _json_or_raise(self, response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            data = {}
        error = data.get("error")
        if response.status_code == 401 or (isinstance(error, dict) and error.get("code") == 190):
            raise AuthorizationExpired(
                self._error_message(error, "Instagram authorization expired"),
                status=response.status_code,
            )
        if isinstance(error, dict):
            code = _int_or_none(error.get("code"))
            message = self._error_message(error, "Instagram API error")
            if code in {4, 17, 32, 613} or response.status_code == 429:
                raise RateLimited(message, status=response.status_code)
            if code is not None and (code == 10 or 200 <= code <= 299):
                raise PermissionMissing(message, status=response.status_code)
            raise ProviderError(
                message,
                retryable=response.status_code >= 500,
                status=response.status_code,
            )
        if response.status_code == 429:
            raise RateLimited("Instagram rate limit exceeded", status=429)
        if response.status_code >= 400:
            raise ProviderError(
                f"Instagram request failed with HTTP {response.status_code}",
                retryable=response.status_code >= 500,
                status=response.status_code,
            )
        return data

    @staticmethod
    def _error_message(error: object, fallback: str) -> str:
        if isinstance(error, dict):
            for key in ("message", "error_user_msg"):
                value = error.get(key)
                if isinstance(value, str) and value:
                    return value
        return fallback

    def _graph_get(
        self,
        token: TokenSet,
        path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        response = provider_request(
            self.client,
            "Instagram",
            "GET",
            f"{GRAPH_BASE}/{path.lstrip('/')}",
            params=params,
            headers={"Authorization": f"Bearer {token.access_token}"},
        )
        return self._json_or_raise(response)

    # --------------------------------------------------------------- identity

    def fetch_identity(self, token: TokenSet) -> AccountIdentity:
        data = self._graph_get(
            token,
            "me",
            {
                "fields": (
                    "user_id,username,name,account_type,profile_picture_url,"
                    "followers_count,follows_count,media_count"
                )
            },
        )
        username = data.get("username")
        external_id = str(data.get("user_id") or data.get("id") or "")
        return AccountIdentity(
            external_account_id=external_id,
            handle=username if isinstance(username, str) else None,
            display_name=str(data["name"]) if data.get("name") else None,
            avatar_url=(
                str(data["profile_picture_url"]) if data.get("profile_picture_url") else None
            ),
            profile_url=(
                f"https://www.instagram.com/{username}/" if isinstance(username, str) else None
            ),
            account_type=(str(data["account_type"]) if data.get("account_type") else None),
            followers=_int_or_none(data.get("followers_count")),
            following=_int_or_none(data.get("follows_count")),
            content_count=_int_or_none(data.get("media_count")),
            raw=data,
        )

    def fetch_account_totals(self, token: TokenSet) -> dict[str, int | float | None]:
        """Total-value insights for the account snapshot; failures are warnings."""
        data = self._graph_get(
            token,
            "me/insights",
            {
                "metric": _TOTAL_VALUE_METRICS,
                "period": "day",
                "metric_type": "total_value",
            },
        )
        totals: dict[str, int | float | None] = {}
        entries = data.get("data")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            total = entry.get("total_value")
            value = total.get("value") if isinstance(total, dict) else None
            totals[str(entry["name"])] = _int_or_none(value) or (
                float(value) if isinstance(value, (int, float)) else None
            )
        return totals

    # --------------------------------------------------------------- content

    def list_content(
        self,
        token: TokenSet,
        *,
        cursor: dict[str, Any] | None,
        limit: int,
    ) -> Page[ContentItem]:
        params: dict[str, Any] = {
            "fields": (
                "id,caption,media_type,media_product_type,media_url,permalink,"
                "thumbnail_url,timestamp,like_count,comments_count"
            ),
            "limit": min(max(limit, 1), 100),
        }
        if cursor and cursor.get("after"):
            params["after"] = str(cursor["after"])
        data = self._graph_get(token, "me/media", params)
        items: list[ContentItem] = []
        entries = data.get("data")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            media_product = str(entry.get("media_product_type") or "")
            media_type = str(entry.get("media_type") or "")
            content_type = _CONTENT_TYPES.get(
                media_product if media_product in _CONTENT_TYPES else media_type,
                "unknown",
            )
            items.append(
                ContentItem(
                    external_id=str(entry.get("id") or ""),
                    content_type=content_type,
                    caption=str(entry["caption"]) if entry.get("caption") else None,
                    permalink=(str(entry["permalink"]) if entry.get("permalink") else None),
                    thumbnail_url=(
                        str(entry.get("thumbnail_url") or entry.get("media_url") or "") or None
                    ),
                    published_at=_parse_timestamp(
                        str(entry["timestamp"]) if entry.get("timestamp") else None
                    ),
                    metrics={
                        "likes": _int_or_none(entry.get("like_count")),
                        "comments": _int_or_none(entry.get("comments_count")),
                    },
                    raw=entry,
                )
            )
        next_cursor: dict[str, Any] | None = None
        paging = data.get("paging")
        if isinstance(paging, dict):
            cursors = paging.get("cursors")
            if isinstance(cursors, dict) and cursors.get("after") and items:
                next_cursor = {"after": str(cursors["after"])}
        return Page(items=items, next_cursor=next_cursor)

    def fetch_content_metrics(
        self,
        token: TokenSet,
        external_ids: list[str],
    ) -> dict[str, dict[str, int | float | None]]:
        metrics: dict[str, dict[str, int | float | None]] = {}
        for external_id in external_ids:
            metrics[external_id] = self._content_insights(token, external_id)
        return metrics

    def _content_insights(self, token: TokenSet, external_id: str) -> dict[str, int | float | None]:
        product = self._media_product_type(token, external_id)
        metric = _STORY_METRICS if product == "STORY" else _CONTENT_METRICS
        try:
            data = self._graph_get(token, f"{external_id}/insights", {"metric": metric})
        except PermissionMissing as exc:
            self.warnings.append(f"insights unavailable for media {external_id}: {exc.detail}")
            return {}
        except ProviderError as exc:
            if exc.status == 400:
                self.warnings.append(f"insights unavailable for media {external_id}: {exc.detail}")
                return {}
            raise
        values: dict[str, int | float | None] = {}
        entries = data.get("data")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            metric_values = entry.get("values")
            if isinstance(metric_values, list) and metric_values:
                value = (
                    metric_values[0].get("value") if isinstance(metric_values[0], dict) else None
                )
            else:
                value = entry.get("value")
            if isinstance(value, (int, float)):
                values[str(entry["name"])] = int(value)
            elif value is not None:
                try:
                    values[str(entry["name"])] = float(value)
                except (TypeError, ValueError):
                    continue
        return values

    def _media_product_type(self, token: TokenSet, external_id: str) -> str | None:
        data = self._graph_get(token, external_id, {"fields": "media_type,media_product_type"})
        for key in ("media_product_type", "media_type"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
        return None

    # --------------------------------------------------------------- metrics

    def fetch_account_daily_metrics(
        self,
        token: TokenSet,
        *,
        since: date,
        until: date,
    ) -> list[DailyMetric]:
        since_unix = int(datetime(since.year, since.month, since.day, tzinfo=UTC).timestamp())
        until_unix = (
            int(datetime(until.year, until.month, until.day, tzinfo=UTC).timestamp()) + 86399
        )
        data = self._graph_get(
            token,
            "me/insights",
            {
                "metric": _DAILY_METRICS,
                "period": "day",
                "since": since_unix,
                "until": until_unix,
            },
        )
        results: list[DailyMetric] = []
        entries = data.get("data")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict) or not entry.get("name"):
                continue
            metric = str(entry["name"])
            values = entry.get("values")
            for point in values if isinstance(values, list) else []:
                if not isinstance(point, dict):
                    continue
                end_time = point.get("end_time")
                parsed = _parse_timestamp(str(end_time)) if end_time else None
                day = parsed.date() if parsed else None
                value = point.get("value")
                if day is None or not isinstance(value, (int, float)):
                    continue
                results.append(
                    DailyMetric(
                        day=day,
                        metric=metric,
                        value=float(value),
                        source="instagram_insights_api",
                    )
                )
        return results

    # -------------------------------------------------------------- comments

    def list_comments(
        self,
        token: TokenSet,
        *,
        content_external_id: str,
        cursor: dict[str, Any] | None,
        limit: int,
    ) -> Page[CommentItem]:
        owner_id = token.extra.get("user_id", "")
        params: dict[str, Any] = {
            "fields": (
                "id,text,username,timestamp,like_count,from{id,username},"
                "replies{id,text,username,timestamp,like_count,from{id,username}}"
            ),
            "limit": min(max(limit, 1), 100),
        }
        if cursor and cursor.get("after"):
            params["after"] = str(cursor["after"])
        data = self._graph_get(token, f"{content_external_id}/comments", params)
        comments: list[CommentItem] = []
        entries = data.get("data")
        for entry in entries if isinstance(entries, list) else []:
            if not isinstance(entry, dict):
                continue
            top_id = str(entry.get("id") or "")
            comments.append(
                self._comment_from_entry(
                    entry,
                    content_external_id=content_external_id,
                    parent_external_id=None,
                    owner_id=owner_id,
                )
            )
            replies = entry.get("replies")
            reply_entries = replies.get("data") if isinstance(replies, dict) else None
            for reply in reply_entries if isinstance(reply_entries, list) else []:
                if not isinstance(reply, dict):
                    continue
                comments.append(
                    self._comment_from_entry(
                        reply,
                        content_external_id=content_external_id,
                        parent_external_id=top_id,
                        owner_id=owner_id,
                    )
                )
        next_cursor: dict[str, Any] | None = None
        paging = data.get("paging")
        if isinstance(paging, dict):
            cursors = paging.get("cursors")
            if isinstance(cursors, dict) and cursors.get("after") and entries:
                next_cursor = {"after": str(cursors["after"])}
        return Page(items=comments, next_cursor=next_cursor)

    @staticmethod
    def _comment_from_entry(
        entry: dict[str, Any],
        *,
        content_external_id: str,
        parent_external_id: str | None,
        owner_id: str,
    ) -> CommentItem:
        author = entry.get("from")
        author_id = str(author.get("id")) if isinstance(author, dict) and author.get("id") else None
        username = (
            str(author.get("username"))
            if isinstance(author, dict) and author.get("username")
            else (str(entry["username"]) if entry.get("username") else None)
        )
        return CommentItem(
            external_id=str(entry.get("id") or ""),
            content_external_id=content_external_id,
            parent_external_id=parent_external_id,
            author_name=username,
            author_external_id=author_id,
            author_is_owner=bool(author_id) and author_id == owner_id,
            text=str(entry.get("text") or ""),
            like_count=_int_or_none(entry.get("like_count")),
            published_at=_parse_timestamp(
                str(entry["timestamp"]) if entry.get("timestamp") else None
            ),
            raw=entry,
        )

    def reply_to_comment(
        self,
        token: TokenSet,
        *,
        comment_external_id: str,
        text: str,
    ) -> ReplyResult:
        if not self.settings.inbox_replies_enabled:
            raise CapabilityUnsupported("comment replies require RUNWAY_INBOX_REPLIES_ENABLED=true")
        response = provider_request(
            self.client,
            "Instagram",
            "POST",
            f"{GRAPH_BASE}/{comment_external_id}/replies",
            data={"message": text},
            headers={"Authorization": f"Bearer {token.access_token}"},
        )
        data = self._json_or_raise(response)
        reply_id = str(data.get("id") or "")
        verified = False
        if reply_id:
            # The reply is already public; a failed read-back only leaves it
            # unverified.
            try:
                check = self._graph_get(token, reply_id, {"fields": "id,text,timestamp"})
            except ProviderError:
                check = {}
            verified = str(check.get("id") or "") == reply_id
        return ReplyResult(
            external_id=reply_id,
            verified=verified,
            detail="reply posted" if verified else "reply posted but not yet visible",
        )
