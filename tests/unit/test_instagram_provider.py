from __future__ import annotations

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
from runway.platforms.instagram import InstagramProvider


def _provider(handler) -> InstagramProvider:
    settings = Settings(
        instagram_app_id="ig-app",
        instagram_app_secret="ig-secret",
        inbox_replies_enabled=True,
    )
    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=20.0)
    return InstagramProvider(settings, client)


def _token() -> TokenSet:
    return TokenSet(access_token="ig-access", extra={"user_id": "17841400923817"})


def test_exchange_code_handles_wrapped_shape_and_strips_suffix() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.instagram.com":
            form = parse_qs(request.content.decode())
            seen["code"] = form["code"][0]
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "access_token": "short-token",
                            "user_id": 17841400923817,
                            "permissions": ["instagram_business_basic"],
                        }
                    ]
                },
            )
        if request.url.path == "/access_token":
            query = parse_qs(urlparse(str(request.url)).query)
            assert query["grant_type"] == ["ig_exchange_token"]
            assert query["access_token"] == ["short-token"]
            return httpx.Response(200, json={"access_token": "long-token", "expires_in": 5184000})
        return httpx.Response(404, json={})

    provider = _provider(handler)
    token = provider.exchange_code(
        code="authcode#_", redirect_uri="http://x/cb", code_verifier=None
    )
    assert seen["code"] == "authcode"
    assert token.access_token == "long-token"
    assert token.expires_at is not None


def test_exchange_code_handles_flat_shape() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.instagram.com":
            return httpx.Response(200, json={"access_token": "flat-short", "user_id": 42})
        if request.url.path == "/access_token":
            return httpx.Response(200, json={"access_token": "flat-long", "expires_in": 5184000})
        return httpx.Response(404, json={})

    provider = _provider(handler)
    token = provider.exchange_code(code="code", redirect_uri="http://x/cb", code_verifier=None)
    assert token.access_token == "flat-long"


def test_exchange_code_falls_back_to_requested_scopes_when_permissions_omitted() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.instagram.com":
            return httpx.Response(200, json={"access_token": "short", "user_id": 1})
        if request.url.path == "/access_token":
            return httpx.Response(200, json={"access_token": "long", "expires_in": 5184000})
        return httpx.Response(404, json={})

    provider = _provider(handler)
    token = provider.exchange_code(code="code", redirect_uri="http://x/cb", code_verifier=None)
    assert token.scopes == [
        "instagram_business_basic",
        "instagram_business_manage_insights",
        "instagram_business_manage_comments",
    ]


def test_refresh_uses_refresh_endpoint_with_bearer_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlparse(str(request.url)).query)
        assert request.url.path == "/refresh_access_token"
        assert query["grant_type"] == ["ig_refresh_token"]
        assert "access_token" not in query
        assert request.headers["Authorization"] == "Bearer ig-access"
        return httpx.Response(200, json={"access_token": "refreshed", "expires_in": 5184000})

    provider = _provider(handler)
    token = provider.refresh(_token())
    assert token.access_token == "refreshed"
    assert token.expires_at is not None


def test_graph_get_sends_bearer_token_never_in_query() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlparse(str(request.url)).query)
        assert "access_token" not in query
        assert request.headers["Authorization"] == "Bearer ig-access"
        return httpx.Response(
            200,
            json={"user_id": "17841400923817", "username": "northwindstudio"},
        )

    provider = _provider(handler)
    identity = provider.fetch_identity(_token())
    assert identity.external_account_id == "17841400923817"


def test_transport_error_raises_provider_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("network unreachable", request=request)

    provider = _provider(handler)
    with pytest.raises(ProviderError):
        provider.fetch_identity(_token())


def test_identity_parses_me() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v25.0/me"
        return httpx.Response(
            200,
            json={
                "user_id": "17841400923817",
                "username": "northwindstudio",
                "name": "Northwind Studio",
                "account_type": "BUSINESS",
                "profile_picture_url": "https://img/ig.jpg",
                "followers_count": 58240,
                "follows_count": 341,
                "media_count": 212,
            },
        )

    provider = _provider(handler)
    identity = provider.fetch_identity(_token())
    assert identity.external_account_id == "17841400923817"
    assert identity.handle == "northwindstudio"
    assert identity.followers == 58240
    assert identity.following == 341
    assert identity.content_count == 212
    assert identity.account_type == "BUSINESS"


def _media_entry(idx: int, media_type: str) -> dict:
    return {
        "id": f"ig{idx}",
        "caption": f"cap {idx}",
        "media_type": media_type,
        "media_product_type": media_type,
        "permalink": f"https://inst/p/ig{idx}",
        "thumbnail_url": f"https://img/ig{idx}.jpg",
        "timestamp": "2026-10-01T12:00:00+0000",
        "like_count": idx * 10,
        "comments_count": idx,
    }


def test_media_paging_uses_after_cursor_and_maps_types() -> None:
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlparse(str(request.url)).query)
        calls.append(query.get("after", [None])[0])
        if "after" not in query:
            return httpx.Response(
                200,
                json={
                    "data": [_media_entry(1, "REELS"), _media_entry(2, "IMAGE")],
                    "paging": {"cursors": {"after": "cursor2"}},
                },
            )
        return httpx.Response(
            200,
            json={
                "data": [_media_entry(3, "CAROUSEL_ALBUM"), _media_entry(4, "STORY")],
                "paging": {"cursors": {"after": "cursor3"}},
            },
        )

    provider = _provider(handler)
    first = provider.list_content(_token(), cursor=None, limit=50)
    assert first.next_cursor == {"after": "cursor2"}
    second = provider.list_content(_token(), cursor=first.next_cursor, limit=50)
    assert calls == [None, "cursor2"]
    types = [item.content_type for item in first.items + second.items]
    assert types == ["reel", "image", "carousel", "story"]


def test_insights_failure_is_a_warning_not_an_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v25.0/ig1":
            return httpx.Response(200, json={"media_type": "IMAGE", "media_product_type": "IMAGE"})
        if request.url.path == "/v25.0/ig1/insights":
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "metric not supported for this media",
                        "code": 100,
                    }
                },
            )
        return httpx.Response(404, json={})

    provider = _provider(handler)
    metrics = provider.fetch_content_metrics(_token(), ["ig1"])
    assert metrics["ig1"] == {}
    assert provider.warnings


def test_comments_include_replies_and_owner_flag() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "c1",
                        "text": "nice",
                        "username": "aster",
                        "timestamp": "2026-10-05T10:00:00+0000",
                        "like_count": 3,
                        "from": {"id": "u1", "username": "aster"},
                        "replies": {
                            "data": [
                                {
                                    "id": "c1r1",
                                    "text": "thanks",
                                    "timestamp": "2026-10-05T11:00:00+0000",
                                    "from": {
                                        "id": "17841400923817",
                                        "username": "northwindstudio",
                                    },
                                }
                            ]
                        },
                    }
                ]
            },
        )

    provider = _provider(handler)
    page = provider.list_comments(_token(), content_external_id="ig1", cursor=None, limit=50)
    assert len(page.items) == 2
    assert page.items[0].author_is_owner is False
    assert page.items[1].parent_external_id == "c1"
    assert page.items[1].author_is_owner is True


def test_reply_posts_form_and_verifies() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and request.url.path == "/v25.0/c1/replies":
            seen["body"] = request.content.decode()
            return httpx.Response(200, json={"id": "r9"})
        if request.url.path == "/v25.0/r9":
            return httpx.Response(200, json={"id": "r9", "text": "hey"})
        return httpx.Response(404, json={})

    provider = _provider(handler)
    result = provider.reply_to_comment(_token(), comment_external_id="c1", text="hey")
    assert "message=hey" in seen["body"]
    assert result.verified is True


def test_code_190_raises_authorization_expired() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": {"message": "token expired", "code": 190}})

    provider = _provider(handler)
    with pytest.raises(AuthorizationExpired):
        provider.fetch_identity(_token())


def test_http_429_raises_rate_limited() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "rate limited", "code": 4}})

    provider = _provider(handler)
    with pytest.raises(RateLimited):
        provider.fetch_identity(_token())
