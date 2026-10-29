from __future__ import annotations

import re
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
    YOUTUBE_ANALYTICS_SCOPE,
    YOUTUBE_ENGAGE_SCOPE,
    YOUTUBE_READONLY_SCOPE,
    CapabilityEntry,
    Platform,
    capability_matrix,
)

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
API_BASE = "https://www.googleapis.com/youtube/v3"
ANALYTICS_BASE = "https://youtubeanalytics.googleapis.com/v2"

_DURATION_PATTERN = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?"
    r"(?P<seconds>\d+(?:\.\d+)?)S)?$"
)
SHORT_MAX_SECONDS = 180


def parse_iso8601_duration(value: str | None) -> int | None:
    if not value:
        return None
    match = _DURATION_PATTERN.match(value)
    if not match:
        return None
    days = int(match.group("days") or 0)
    hours = int(match.group("hours") or 0)
    minutes = int(match.group("minutes") or 0)
    seconds = float(match.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + int(seconds)


def _parse_rfc3339(value: str | None) -> datetime | None:
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


def _as_dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _as_str_or_none(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


class YouTubeProvider(PlatformProvider):
    platform = Platform.youtube

    def __init__(self, settings: Settings, client: httpx.Client | None = None) -> None:
        self.settings = settings
        self.client = client or httpx.Client(timeout=20.0)

    def capabilities(self) -> list[CapabilityEntry]:
        return capability_matrix(
            Platform.youtube,
            engage_scopes=self.settings.youtube_engage_scopes,
            replies_enabled=self.settings.inbox_replies_enabled,
        )

    def configured(self) -> bool:
        return self.settings.youtube_oauth_configured

    def _scopes(self) -> list[str]:
        scopes = [YOUTUBE_READONLY_SCOPE, YOUTUBE_ANALYTICS_SCOPE]
        if self.settings.youtube_engage_scopes:
            scopes.append(YOUTUBE_ENGAGE_SCOPE)
        return scopes

    # ------------------------------------------------------------------ OAuth

    def authorization_url(
        self,
        *,
        state: str,
        redirect_uri: str,
        code_challenge: str | None,
    ) -> str:
        params: dict[str, str] = {
            "client_id": self.settings.youtube_client_id or "",
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self._scopes()),
            "access_type": "offline",
            "prompt": "consent",
            "include_granted_scopes": "true",
            "state": state,
        }
        if code_challenge:
            params["code_challenge"] = code_challenge
            params["code_challenge_method"] = "S256"
        return f"{AUTH_URL}?{httpx.QueryParams(params)}"

    def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None,
    ) -> TokenSet:
        payload: dict[str, str] = {
            "client_id": self.settings.youtube_client_id or "",
            "client_secret": self.settings.youtube_client_secret or "",
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        if code_verifier:
            payload["code_verifier"] = code_verifier
        response = provider_request(self.client, "YouTube", "POST", TOKEN_URL, data=payload)
        data = self._json_or_raise(response)
        return self._token_from_response(data, fallback_scopes=self._scopes())

    def refresh(self, token: TokenSet) -> TokenSet:
        if not token.refresh_token:
            raise AuthorizationExpired("YouTube refresh token is missing", status=401)
        response = provider_request(
            self.client,
            "YouTube",
            "POST",
            TOKEN_URL,
            data={
                "client_id": self.settings.youtube_client_id or "",
                "client_secret": self.settings.youtube_client_secret or "",
                "refresh_token": token.refresh_token,
                "grant_type": "refresh_token",
            },
        )
        data = self._json_or_raise(response)
        refreshed = self._token_from_response(data, fallback_scopes=token.scopes)
        refreshed.refresh_token = token.refresh_token
        return refreshed

    def revoke(self, token: TokenSet) -> bool:
        # RFC 7009: the token is sent in the form-encoded body, never in the URL.
        response = provider_request(
            self.client,
            "YouTube",
            "POST",
            REVOKE_URL,
            data={"token": token.access_token},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        return response.is_success

    def _token_from_response(self, data: dict[str, Any], *, fallback_scopes: list[str]) -> TokenSet:
        access_token = data.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ProviderError("YouTube token response did not include access_token")
        expires_in = _int_or_none(data.get("expires_in"))
        scope_value = data.get("scope")
        scopes = (
            scope_value.split()
            if isinstance(scope_value, str) and scope_value.strip()
            else fallback_scopes
        )
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
            token_type=str(data.get("token_type") or "Bearer"),
        )

    # -------------------------------------------------------------- HTTP core

    def _json_or_raise(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code == 401:
            raise AuthorizationExpired("YouTube authorization expired or was revoked", status=401)
        try:
            data = response.json()
        except ValueError:
            data = {}
        if isinstance(data, dict) and response.status_code >= 400:
            self._raise_for_error_payload(response.status_code, data)
        if response.status_code >= 400:
            raise ProviderError(
                f"YouTube request failed with HTTP {response.status_code}",
                retryable=response.status_code >= 500,
                status=response.status_code,
            )
        if not isinstance(data, dict):
            raise ProviderError("YouTube returned a non-object response")
        return data

    def _raise_for_error_payload(self, status_code: int, data: dict[str, Any]) -> None:
        message = "YouTube API error"
        reason: str | None = None
        error = data.get("error")
        if isinstance(error, str):
            description = data.get("error_description")
            message = str(description) if isinstance(description, str) else error
            if error == "invalid_grant":
                raise AuthorizationExpired(message, status=status_code)
            raise ProviderError(message, status=status_code)
        if isinstance(error, dict):
            if isinstance(error.get("message"), str):
                message = str(error["message"])
            errors = error.get("errors")
            if isinstance(errors, list) and errors:
                first = errors[0]
                if isinstance(first, dict) and isinstance(first.get("reason"), str):
                    reason = str(first["reason"])
            code = error.get("code")
            if code == "invalid_grant" or (
                isinstance(error.get("message"), str) and "invalid_grant" in message
            ):
                raise AuthorizationExpired(message, status=status_code)
        detail = f"{message} [{reason}]" if reason else message
        if status_code == 403:
            if reason in {"quotaExceeded", "rateLimitExceeded", "dailyLimitExceeded"}:
                raise RateLimited(detail, status=403)
            raise PermissionMissing(detail, status=403)
        raise ProviderError(message, retryable=status_code >= 500, status=status_code)

    def _api_get(
        self,
        token: TokenSet,
        url: str,
        params: dict[str, Any],
        *,
        quota_units: int,
    ) -> tuple[dict[str, Any], int]:
        response = provider_request(
            self.client,
            "YouTube",
            "GET",
            url,
            params=params,
            headers={"Authorization": f"Bearer {token.access_token}"},
        )
        return self._json_or_raise(response), quota_units

    # --------------------------------------------------------------- identity

    def fetch_identity(self, token: TokenSet) -> AccountIdentity:
        data, _ = self._api_get(
            token,
            f"{API_BASE}/channels",
            {"part": "snippet,statistics,contentDetails", "mine": "true"},
            quota_units=1,
        )
        items = data.get("items")
        if not isinstance(items, list) or not items:
            raise ProviderError("YouTube channels.list returned no channel")
        channel = _as_dict(items[0])
        snippet = _as_dict(channel.get("snippet"))
        statistics = _as_dict(channel.get("statistics"))
        content_details = _as_dict(channel.get("contentDetails"))
        thumbnails = _as_dict(snippet.get("thumbnails"))
        default_thumb = _as_dict(thumbnails.get("default"))
        related = _as_dict(content_details.get("relatedPlaylists"))
        channel_id = str(channel.get("id") or "")
        return AccountIdentity(
            external_account_id=channel_id,
            handle=_as_str_or_none(snippet.get("customUrl")),
            display_name=_as_str_or_none(snippet.get("title")),
            avatar_url=_as_str_or_none(default_thumb.get("url")),
            profile_url=(f"https://www.youtube.com/channel/{channel_id}" if channel_id else None),
            account_type="channel",
            followers=_int_or_none(statistics.get("subscriberCount")),
            content_count=_int_or_none(statistics.get("videoCount")),
            total_views=_int_or_none(statistics.get("viewCount")),
            raw={**channel, "uploads_playlist": related.get("uploads")},
        )

    # --------------------------------------------------------------- content

    def _uploads_playlist(self, token: TokenSet) -> str:
        uploads = token.extra.get("uploads_playlist", "")
        if uploads:
            return uploads
        identity = self.fetch_identity(token)
        fetched = identity.raw.get("uploads_playlist")
        if not isinstance(fetched, str) or not fetched:
            raise ProviderError("YouTube channel has no uploads playlist")
        return fetched

    def list_content(
        self,
        token: TokenSet,
        *,
        cursor: dict[str, Any] | None,
        limit: int,
    ) -> Page[ContentItem]:
        uploads = self._uploads_playlist(token)
        params: dict[str, Any] = {
            "part": "snippet,contentDetails",
            "playlistId": uploads,
            "maxResults": min(max(limit, 1), 50),
        }
        if cursor and cursor.get("page_token"):
            params["pageToken"] = str(cursor["page_token"])
        data, units = self._api_get(token, f"{API_BASE}/playlistItems", params, quota_units=1)
        items = data.get("items")
        video_ids: list[str] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            details = item.get("contentDetails")
            if isinstance(details, dict) and details.get("videoId"):
                video_ids.append(str(details["videoId"]))
        videos = self._video_details(token, video_ids)
        content_items = [self._content_from_video(video) for video in videos]
        next_token = data.get("nextPageToken")
        return Page(
            items=content_items,
            next_cursor=({"page_token": str(next_token)} if isinstance(next_token, str) else None),
            quota_units=units,
        )

    def _video_details(self, token: TokenSet, video_ids: list[str]) -> list[dict[str, Any]]:
        videos: list[dict[str, Any]] = []
        for index in range(0, len(video_ids), 50):
            batch = video_ids[index : index + 50]
            if not batch:
                continue
            data, _ = self._api_get(
                token,
                f"{API_BASE}/videos",
                {
                    "part": "snippet,contentDetails,statistics,status",
                    "id": ",".join(batch),
                },
                quota_units=1,
            )
            items = data.get("items")
            if isinstance(items, list):
                videos.extend(item for item in items if isinstance(item, dict))
        return videos

    def _content_from_video(self, video: dict[str, Any]) -> ContentItem:
        snippet = _as_dict(video.get("snippet"))
        details = _as_dict(video.get("contentDetails"))
        statistics = _as_dict(video.get("statistics"))
        status = _as_dict(video.get("status"))
        duration = parse_iso8601_duration(_as_str_or_none(details.get("duration")))
        video_id = str(video.get("id") or "")
        return ContentItem(
            external_id=video_id,
            content_type=(
                "short" if duration is not None and duration <= SHORT_MAX_SECONDS else "video"
            ),
            title=_as_str_or_none(snippet.get("title")),
            caption=_as_str_or_none(snippet.get("description")),
            permalink=f"https://www.youtube.com/watch?v={video_id}" if video_id else None,
            thumbnail_url=self._best_thumbnail(snippet),
            published_at=_parse_rfc3339(_as_str_or_none(snippet.get("publishedAt"))),
            duration_seconds=duration,
            privacy_status=_as_str_or_none(status.get("privacyStatus")),
            metrics={
                "views": _int_or_none(statistics.get("viewCount")),
                "likes": _int_or_none(statistics.get("likeCount")),
                "comments": _int_or_none(statistics.get("commentCount")),
            },
            raw=video,
        )

    @staticmethod
    def _best_thumbnail(snippet: dict[str, Any]) -> str | None:
        thumbnails = _as_dict(snippet.get("thumbnails"))
        for key in ("high", "medium", "default", "standard", "maxres"):
            entry = thumbnails.get(key)
            if isinstance(entry, dict) and entry.get("url"):
                return str(entry["url"])
        return None

    def fetch_content_metrics(
        self,
        token: TokenSet,
        external_ids: list[str],
    ) -> dict[str, dict[str, int | float | None]]:
        metrics: dict[str, dict[str, int | float | None]] = {}
        for video in self._video_details(token, external_ids):
            statistics = _as_dict(video.get("statistics"))
            metrics[str(video.get("id") or "")] = {
                "views": _int_or_none(statistics.get("viewCount")),
                "likes": _int_or_none(statistics.get("likeCount")),
                "comments": _int_or_none(statistics.get("commentCount")),
            }
        return metrics

    # --------------------------------------------------------------- metrics

    def fetch_account_daily_metrics(
        self,
        token: TokenSet,
        *,
        since: date,
        until: date,
    ) -> list[DailyMetric]:
        data, _ = self._api_get(
            token,
            f"{ANALYTICS_BASE}/reports",
            {
                "ids": "channel==MINE",
                "startDate": since.isoformat(),
                "endDate": until.isoformat(),
                "metrics": (
                    "views,estimatedMinutesWatched,averageViewDuration,"
                    "subscribersGained,subscribersLost,likes,comments,shares"
                ),
                "dimensions": "day",
                "sort": "day",
            },
            quota_units=1,
        )
        headers = data.get("columnHeaders")
        names = (
            [
                str(header.get("name"))
                for header in headers
                if isinstance(header, dict) and header.get("name")
            ]
            if isinstance(headers, list)
            else []
        )
        results: list[DailyMetric] = []
        rows = data.get("rows")
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, list) or not row:
                continue
            day_text = str(row[0])
            try:
                day = date.fromisoformat(day_text)
            except ValueError:
                continue
            for column_index, name in enumerate(names[1:], start=1):
                if column_index >= len(row):
                    break
                try:
                    value = float(row[column_index])
                except (TypeError, ValueError):
                    continue
                results.append(
                    DailyMetric(
                        day=day,
                        metric=name,
                        value=value,
                        source="youtube_analytics_api",
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
        channel_id = token.extra.get("channel_id", "")
        params: dict[str, Any] = {
            "part": "snippet,replies",
            "videoId": content_external_id,
            "maxResults": min(max(limit, 1), 100),
            "order": "time",
            "textFormat": "plainText",
        }
        if cursor and cursor.get("page_token"):
            params["pageToken"] = str(cursor["page_token"])
        try:
            data, units = self._api_get(token, f"{API_BASE}/commentThreads", params, quota_units=1)
        except PermissionMissing as exc:
            if "commentsDisabled" in exc.detail:
                return Page(items=[], next_cursor=None, quota_units=1)
            raise
        comments: list[CommentItem] = []
        items = data.get("items")
        for thread in items if isinstance(items, list) else []:
            if not isinstance(thread, dict):
                continue
            thread_snippet = _as_dict(thread.get("snippet"))
            top = thread_snippet.get("topLevelComment")
            if not isinstance(top, dict):
                continue
            top_id = str(thread.get("id") or top.get("id") or "")
            total_replies = _int_or_none(thread_snippet.get("totalReplyCount"))
            comments.append(
                self._comment_from_snippet(
                    _as_dict(top.get("snippet")),
                    external_id=top_id,
                    content_external_id=content_external_id,
                    parent_external_id=None,
                    channel_id=channel_id,
                    reply_count=total_replies,
                    raw=dict(top),
                )
            )
            replies = _as_dict(thread.get("replies"))
            reply_items = replies.get("comments")
            for reply in reply_items if isinstance(reply_items, list) else []:
                if not isinstance(reply, dict):
                    continue
                comments.append(
                    self._comment_from_snippet(
                        _as_dict(reply.get("snippet")),
                        external_id=str(reply.get("id") or ""),
                        content_external_id=content_external_id,
                        parent_external_id=top_id,
                        channel_id=channel_id,
                        reply_count=None,
                        raw=dict(reply),
                    )
                )
        next_token = data.get("nextPageToken")
        return Page(
            items=comments,
            next_cursor=({"page_token": str(next_token)} if isinstance(next_token, str) else None),
            quota_units=units,
        )

    @staticmethod
    def _comment_from_snippet(
        snippet: dict[str, Any],
        *,
        external_id: str,
        content_external_id: str,
        parent_external_id: str | None,
        channel_id: str,
        reply_count: int | None,
        raw: dict[str, Any],
    ) -> CommentItem:
        author_channel = snippet.get("authorChannelId")
        author_id = (
            str(author_channel.get("value"))
            if isinstance(author_channel, dict) and author_channel.get("value")
            else None
        )
        return CommentItem(
            external_id=external_id,
            content_external_id=content_external_id,
            parent_external_id=parent_external_id,
            author_name=(
                str(snippet["authorDisplayName"]) if snippet.get("authorDisplayName") else None
            ),
            author_external_id=author_id,
            author_avatar_url=(
                str(snippet["authorProfileImageUrl"])
                if snippet.get("authorProfileImageUrl")
                else None
            ),
            author_is_owner=bool(author_id) and author_id == channel_id,
            text=str(snippet.get("textDisplay") or snippet.get("textOriginal") or ""),
            like_count=_int_or_none(snippet.get("likeCount")),
            reply_count=reply_count,
            published_at=_parse_rfc3339(
                str(snippet["publishedAt"]) if snippet.get("publishedAt") else None
            ),
            platform_updated_at=_parse_rfc3339(
                str(snippet["updatedAt"]) if snippet.get("updatedAt") else None
            ),
            raw=raw,
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
            "YouTube",
            "POST",
            f"{API_BASE}/comments",
            params={"part": "snippet"},
            json={"snippet": {"parentId": comment_external_id, "textOriginal": text}},
            headers={"Authorization": f"Bearer {token.access_token}"},
        )
        data = self._json_or_raise(response)
        reply_id = str(data.get("id") or "")
        verified = False
        if reply_id:
            check, _ = self._api_get(
                token,
                f"{API_BASE}/comments",
                {"part": "snippet", "id": reply_id},
                quota_units=1,
            )
            items = check.get("items")
            verified = isinstance(items, list) and any(
                isinstance(item, dict) and str(item.get("id")) == reply_id for item in items
            )
        return ReplyResult(
            external_id=reply_id,
            verified=verified,
            detail="reply posted" if verified else "reply posted but not yet visible",
        )
