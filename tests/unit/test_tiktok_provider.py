from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx
import pytest

from runway.config import Settings
from runway.platforms.base import (
    AuthorizationExpired,
    CapabilityUnsupported,
    PermissionMissing,
    RateLimited,
    TokenSet,
)
from runway.platforms.tiktok import TikTokProvider


def _provider(handler) -> TikTokProvider:
    settings = Settings(
        tiktok_client_key="tt-key",
        tiktok_client_secret="tt-secret",
    )
    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=20.0)
    return TikTokProvider(settings, client)


def _token() -> TokenSet:
    return TokenSet(access_token="tt-access", refresh_token="tt-refresh")


def test_exchange_code_form_fields_and_token_parse() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        seen.update({key: values[0] for key, values in form.items()})
        return httpx.Response(
            200,
            json={
                "access_token": "tt-access",
                "expires_in": 86400,
                "open_id": "openid-1",
                "refresh_token": "tt-refresh",
                "refresh_expires_in": 31536000,
                "scope": "user.info.basic,video.list",
            },
        )

    provider = _provider(handler)
    token = provider.exchange_code(code="code-1", redirect_uri="http://x/cb", code_verifier=None)
    assert seen["grant_type"] == ["authorization_code"][0]
    assert seen["client_key"] == "tt-key"
    assert seen["code"] == "code-1"
    assert token.access_token == "tt-access"
    assert token.refresh_token == "tt-refresh"
    assert token.expires_at is not None
    assert token.extra["open_id"] == "openid-1"
    assert token.scopes == ["user.info.basic", "video.list"]


def test_exchange_code_falls_back_to_requested_scopes_when_omitted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "tt-access",
                "expires_in": 86400,
                "open_id": "openid-1",
                "refresh_token": "tt-refresh",
            },
        )

    provider = _provider(handler)
    token = provider.exchange_code(code="code-1", redirect_uri="http://x/cb", code_verifier=None)
    assert token.scopes == [
        "user.info.basic",
        "user.info.profile",
        "user.info.stats",
        "video.list",
    ]


def test_identity_parses_user() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/user/info/"
        return httpx.Response(
            200,
            json={
                "data": {
                    "user": {
                        "open_id": "openid-1",
                        "display_name": "Northwind Studio",
                        "username": "northwindstudio",
                        "avatar_url": "https://img/tt.jpg",
                        "profile_deep_link": "tiktok://user",
                        "follower_count": 73102,
                        "following_count": 129,
                        "likes_count": 1204407,
                        "video_count": 148,
                    }
                },
                "error": {"code": "ok"},
            },
        )

    provider = _provider(handler)
    identity = provider.fetch_identity(_token())
    assert identity.external_account_id == "openid-1"
    assert identity.handle == "northwindstudio"
    assert identity.followers == 73102
    assert identity.total_likes == 1204407
    assert identity.content_count == 148


def _video(idx: int) -> dict:
    return {
        "id": f"tt{idx}",
        "title": f"Title {idx}",
        "video_description": f"desc {idx}",
        "duration": 30,
        "cover_image_url": f"https://img/tt{idx}.jpg",
        "share_url": f"https://tiktok/v/tt{idx}",
        "create_time": 1759000000 + idx,
        "like_count": idx * 10,
        "comment_count": idx,
        "share_count": idx * 2,
        "view_count": idx * 100,
    }


def test_video_list_cursor_and_has_more_over_two_pages() -> None:
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "videos": [_video(1), _video(2)],
                        "cursor": 42,
                        "has_more": True,
                    },
                    "error": {"code": "ok"},
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {"videos": [_video(3)], "cursor": 0, "has_more": False},
                "error": {"code": "ok"},
            },
        )

    provider = _provider(handler)
    first = provider.list_content(_token(), cursor=None, limit=20)
    assert first.next_cursor == {"cursor": 42}
    second = provider.list_content(_token(), cursor=first.next_cursor, limit=20)
    assert second.next_cursor is None
    assert bodies[0].get("cursor") is None
    assert bodies[1]["cursor"] == 42
    item = first.items[0]
    assert item.content_type == "video"
    assert item.duration_seconds == 30
    assert item.metrics["views"] == 100


def test_error_envelope_maps_error_codes() -> None:
    def make(code: str):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"error": {"code": code, "message": f"err {code}"}})

        return handler

    provider = _provider(make("access_token_invalid"))
    with pytest.raises(AuthorizationExpired):
        provider.fetch_identity(_token())

    provider = _provider(make("rate_limit_exceeded"))
    with pytest.raises(RateLimited):
        provider.fetch_identity(_token())

    provider = _provider(make("scope_not_authorized"))
    with pytest.raises(PermissionMissing):
        provider.fetch_identity(_token())


def test_comments_are_capability_unsupported() -> None:
    provider = _provider(lambda request: httpx.Response(500))
    with pytest.raises(CapabilityUnsupported):
        provider.list_comments(_token(), content_external_id="tt1", cursor=None, limit=10)
    with pytest.raises(CapabilityUnsupported):
        provider.reply_to_comment(_token(), comment_external_id="c1", text="hi")


def test_revoke_returns_false_on_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"code": "invalid_token", "message": "bad"}})

    provider = _provider(handler)
    assert provider.revoke(_token()) is False
