from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from runway.config import Settings
from runway.platforms.base import (
    AuthorizationExpired,
    ProviderError,
    RateLimited,
    TokenSet,
)
from runway.platforms.youtube import YouTubeProvider, parse_iso8601_duration


def _provider(handler) -> YouTubeProvider:
    settings = Settings(
        youtube_client_id="yt-client",
        youtube_client_secret="yt-secret",
        inbox_replies_enabled=True,
    )
    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=20.0)
    return YouTubeProvider(settings, client)


def _token() -> TokenSet:
    return TokenSet(
        access_token="yt-access",
        refresh_token="yt-refresh",
        scopes=["https://www.googleapis.com/auth/youtube.readonly"],
        extra={"channel_id": "UCchan", "uploads_playlist": "UUuploads"},
    )


def _channel_payload() -> dict:
    return {
        "items": [
            {
                "id": "UCchan",
                "snippet": {
                    "title": "Northwind Studio",
                    "customUrl": "@northwindstudio",
                    "thumbnails": {"default": {"url": "https://img/avatar.jpg"}},
                },
                "statistics": {
                    "subscriberCount": "184213",
                    "viewCount": "14203518",
                    "videoCount": "96",
                },
                "contentDetails": {"relatedPlaylists": {"uploads": "UUuploads"}},
            }
        ]
    }


def test_fetch_identity_parses_channel() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/youtube/v3/channels"
        assert "mine=true" in str(request.url.query)
        return httpx.Response(200, json=_channel_payload())

    provider = _provider(handler)
    identity = provider.fetch_identity(_token())
    assert identity.external_account_id == "UCchan"
    assert identity.handle == "northwindstudio"
    assert identity.followers == 184213
    assert identity.total_views == 14203518
    assert identity.content_count == 96
    assert identity.raw["uploads_playlist"] == "UUuploads"


def _playlist_item(video_id: str) -> dict:
    return {
        "snippet": {"title": f"t-{video_id}", "publishedAt": "2026-10-01T12:00:00Z"},
        "contentDetails": {"videoId": video_id},
    }


def _video(video_id: str, duration: str, privacy: str = "public") -> dict:
    return {
        "id": video_id,
        "snippet": {
            "title": f"Video {video_id}",
            "description": "desc",
            "publishedAt": "2026-10-01T12:00:00Z",
            "thumbnails": {"high": {"url": f"https://img/{video_id}.jpg"}},
        },
        "contentDetails": {"duration": duration},
        "statistics": {"viewCount": "1000", "likeCount": "40", "commentCount": "5"},
        "status": {"privacyStatus": privacy},
    }


def test_list_content_paginates_and_batches_video_ids() -> None:
    video_calls: list[list[str]] = []
    pages: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlparse(str(request.url)).query)
        if request.url.path == "/youtube/v3/playlistItems":
            pages.append(query.get("pageToken", [None])[0])
            if "pageToken" not in query:
                return httpx.Response(
                    200,
                    json={
                        "items": [_playlist_item(f"v{i}") for i in range(50)],
                        "nextPageToken": "tok2",
                    },
                )
            return httpx.Response(
                200, json={"items": [_playlist_item("v50"), _playlist_item("v51")]}
            )
        if request.url.path == "/youtube/v3/videos":
            ids = query["id"][0].split(",")
            video_calls.append(ids)
            return httpx.Response(
                200,
                json={
                    "items": [
                        _video(video_id, "PT45S" if video_id == "v0" else "PT10M")
                        for video_id in ids
                    ]
                },
            )
        return httpx.Response(404, json={})

    provider = _provider(handler)
    token = _token()
    first = provider.list_content(token, cursor=None, limit=50)
    assert len(first.items) == 50
    assert first.next_cursor == {"page_token": "tok2"}
    second = provider.list_content(token, cursor=first.next_cursor, limit=50)
    assert len(second.items) == 2
    assert second.next_cursor is None
    assert pages == [None, "tok2"]
    # Each videos.list call carried at most 50 ids.
    assert all(len(ids) <= 50 for ids in video_calls)
    assert video_calls[0][0] == "v0"
    short_item = next(item for item in first.items if item.external_id == "v0")
    assert short_item.content_type == "short"
    assert short_item.duration_seconds == 45
    long_item = next(item for item in first.items if item.external_id == "v1")
    assert long_item.content_type == "video"
    assert long_item.metrics["views"] == 1000


def test_parse_iso8601_duration_variants() -> None:
    assert parse_iso8601_duration("PT1H2M3S") == 3723
    assert parse_iso8601_duration("PT45S") == 45
    assert parse_iso8601_duration("P0D") == 0
    assert parse_iso8601_duration("garbage") is None
    assert parse_iso8601_duration(None) is None


def test_daily_metrics_parse_rows() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/reports"
        return httpx.Response(
            200,
            json={
                "columnHeaders": [
                    {"name": "day"},
                    {"name": "views"},
                    {"name": "likes"},
                ],
                "rows": [["2026-10-10", 100, 4], ["2026-10-11", 120, 6]],
            },
        )

    provider = _provider(handler)
    from datetime import date

    metrics = provider.fetch_account_daily_metrics(
        _token(), since=date(2026, 10, 10), until=date(2026, 10, 11)
    )
    assert len(metrics) == 4
    views = [m for m in metrics if m.metric == "views"]
    assert [m.value for m in views] == [100.0, 120.0]
    assert all(m.source == "youtube_analytics_api" for m in metrics)


def _thread_payload() -> dict:
    return {
        "items": [
            {
                "id": "thread1",
                "snippet": {
                    "totalReplyCount": 1,
                    "topLevelComment": {
                        "id": "c1",
                        "snippet": {
                            "authorDisplayName": "Aster Vale",
                            "authorChannelId": {"value": "UCother"},
                            "textDisplay": "great video",
                            "likeCount": 7,
                            "publishedAt": "2026-10-05T10:00:00Z",
                            "updatedAt": "2026-10-05T10:05:00Z",
                        },
                    },
                },
                "replies": {
                    "comments": [
                        {
                            "id": "c1r1",
                            "snippet": {
                                "authorDisplayName": "Northwind Studio",
                                "authorChannelId": {"value": "UCchan"},
                                "textDisplay": "thanks!",
                                "likeCount": 2,
                                "publishedAt": "2026-10-05T11:00:00Z",
                            },
                        }
                    ]
                },
            }
        ]
    }


def test_comment_threads_include_replies_and_owner_flag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_thread_payload())

    provider = _provider(handler)
    page = provider.list_comments(_token(), content_external_id="v1", cursor=None, limit=100)
    assert len(page.items) == 2
    top = page.items[0]
    reply = page.items[1]
    assert top.external_id == "thread1"
    assert top.author_is_owner is False
    assert top.reply_count == 1
    assert reply.parent_external_id == "thread1"
    assert reply.author_is_owner is True


def test_comments_disabled_returns_empty_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "message": "The video has disabled comments.",
                    "errors": [{"reason": "commentsDisabled"}],
                }
            },
        )

    provider = _provider(handler)
    page = provider.list_comments(_token(), content_external_id="v1", cursor=None, limit=100)
    assert page.items == []
    assert page.next_cursor is None


def test_http_401_raises_authorization_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    provider = _provider(handler)
    with pytest.raises(AuthorizationExpired):
        provider.fetch_identity(_token())


def test_quota_exceeded_raises_rate_limited() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            403,
            json={
                "error": {
                    "message": "quota exceeded",
                    "errors": [{"reason": "quotaExceeded"}],
                }
            },
        )

    provider = _provider(handler)
    with pytest.raises(RateLimited):
        provider.fetch_identity(_token())


def test_invalid_grant_maps_to_authorization_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "expired"})

    provider = _provider(handler)
    with pytest.raises(AuthorizationExpired):
        provider.exchange_code(code="bad", redirect_uri="http://x/cb", code_verifier="v")


def test_reply_posts_correct_body_and_verifies() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/youtube/v3/comments":
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "reply99"})
        if request.url.path == "/youtube/v3/comments":
            return httpx.Response(200, json={"items": [{"id": "reply99"}]})
        return httpx.Response(404, json={})

    provider = _provider(handler)
    result = provider.reply_to_comment(_token(), comment_external_id="thread1", text="hello")
    assert result.external_id == "reply99"
    assert result.verified is True
    assert seen["body"] == {"snippet": {"parentId": "thread1", "textOriginal": "hello"}}


@pytest.mark.parametrize("failure", ["timeout", "server_error"])
def test_reply_that_posted_is_not_reported_failed_when_verification_fails(failure: str) -> None:
    # The POST already created the public reply. A failed read-back used to
    # raise, the inbox recorded the action as failed and left the comment
    # unanswered, and replying again posted a duplicate.
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/youtube/v3/comments":
            return httpx.Response(200, json={"id": "reply99"})
        if failure == "timeout":
            raise httpx.ReadTimeout("read timed out", request=request)
        return httpx.Response(503, json={"error": {"message": "backend error"}})

    provider = _provider(handler)
    result = provider.reply_to_comment(_token(), comment_external_id="thread1", text="hello")
    assert result.external_id == "reply99"
    assert result.verified is False
    assert result.detail == "reply posted but not yet visible"


def test_exchange_code_includes_verifier_and_scopes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        assert form["code_verifier"] == ["verifier-1"]
        assert form["grant_type"] == ["authorization_code"]
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
                "scope": "scope.a scope.b",
            },
        )

    provider = _provider(handler)
    token = provider.exchange_code(
        code="auth-code", redirect_uri="http://x/cb", code_verifier="verifier-1"
    )
    assert token.access_token == "new-access"
    assert token.refresh_token == "new-refresh"
    assert token.scopes == ["scope.a", "scope.b"]
    assert token.expires_at is not None


def test_revoke_posts_token_in_form_body_not_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlparse(str(request.url)).query)
        assert "token" not in query
        form = parse_qs(request.content.decode())
        assert form["token"] == ["yt-access"]
        assert request.headers["Content-Type"].startswith("application/x-www-form-urlencoded")
        return httpx.Response(200)

    provider = _provider(handler)
    assert provider.revoke(_token()) is True


def test_transport_error_raises_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unreachable", request=request)

    provider = _provider(handler)
    with pytest.raises(ProviderError):
        provider.fetch_identity(_token())


def test_exchange_code_falls_back_to_requested_scopes_when_omitted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 3600,
            },
        )

    provider = _provider(handler)
    token = provider.exchange_code(code="auth-code", redirect_uri="http://x/cb", code_verifier="v")
    assert token.scopes == [
        "https://www.googleapis.com/auth/youtube.readonly",
        "https://www.googleapis.com/auth/yt-analytics.readonly",
    ]
