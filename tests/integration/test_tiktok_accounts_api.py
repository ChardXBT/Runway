"""`/api/v1/accounts` TikTok routes: incremental publishing consent and creator-info.

The enable route starts a re-consent that adds `video.upload` (and `video.publish`
only for an audited app). The shared OAuth callback then refuses to attach a
token whose TikTok `open_id` is not the account that asked for the consent
(spec 08 section 1, identity check before every write).
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from runway.api.platform_routes import build_platform_router
from runway.api.v1.accounts_routes import build_accounts_router
from runway.api.v1.errors import install_problem_handlers
from runway.auth.principal import Principal, get_principal
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ConnectedAccount
from runway.platforms.base import TokenSet
from runway.platforms.capabilities import TIKTOK_SCOPES, Platform
from runway.platforms.registry import ProviderRegistry
from runway.platforms.tiktok import TikTokProvider

BASE = "http://127.0.0.1:8000"


def _tiktok_settings(settings: Settings, **update: object) -> Settings:
    return settings.model_copy(
        update={"tiktok_client_key": "tt-key", "tiktok_client_secret": "tt-secret", **update}
    )


def _creator_info(open_id: str = "open-1") -> dict[str, object]:
    return {
        "data": {
            "creator_avatar_url": "https://img/avatar.jpg",
            "creator_username": "northwindstudio",
            "creator_nickname": "Northwind Studio",
            "privacy_level_options": [
                "PUBLIC_TO_EVERYONE",
                "MUTUAL_FOLLOW_FRIENDS",
                "SELF_ONLY",
            ],
            "comment_disabled": False,
            "duet_disabled": True,
            "stitch_disabled": False,
            "max_video_post_duration_sec": 300,
        },
        "error": {"code": "ok"},
    }


class FakeTikTok:
    """A MockTransport handler answering TikTok OAuth, identity and publish API calls."""

    def __init__(self, *, open_id: str = "open-1") -> None:
        self.open_id = open_id
        self.calls: list[tuple[str, str]] = []
        self.requested_scope: str | None = None
        self.creator_info_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, path))
        if path == "/v2/oauth/token/":
            return httpx.Response(
                200,
                json={
                    "access_token": "tt-access",
                    "refresh_token": "tt-refresh",
                    "expires_in": 7200,
                    "refresh_expires_in": 15552000,
                    "open_id": self.open_id,
                    "scope": "user.info.basic,video.list,video.upload",
                },
            )
        if path == "/v2/user/info/":
            return httpx.Response(
                200,
                json={
                    "data": {"user": {"open_id": self.open_id, "display_name": "Northwind"}},
                    "error": {"code": "ok"},
                },
            )
        if path == "/v2/post/publish/creator_info/query/":
            self.creator_info_calls += 1
            return httpx.Response(200, json=_creator_info(self.open_id))
        return httpx.Response(404, json={"error": {"code": "not_found", "message": path}})


@pytest.fixture()
def fake() -> FakeTikTok:
    return FakeTikTok()


@pytest.fixture()
def registry(database: Database, settings: Settings, fake: FakeTikTok) -> ProviderRegistry:
    settings = _tiktok_settings(settings)
    registry = ProviderRegistry(database, settings)
    registry.set_provider(
        Platform.tiktok,
        TikTokProvider(settings, httpx.Client(transport=httpx.MockTransport(fake))),
    )
    return registry


@pytest.fixture()
def client(
    database: Database,
    settings: Settings,
    registry: ProviderRegistry,
    fake: FakeTikTok,
) -> TestClient:
    settings = _tiktok_settings(settings)
    app = FastAPI()
    app.state.settings = settings
    install_problem_handlers(app)
    app.include_router(build_accounts_router(database, settings, registry=registry))
    app.include_router(build_platform_router(database, settings, registry=registry))
    return TestClient(app, base_url=BASE)


def _account(
    database: Database,
    *,
    scopes: list[str] | None = None,
    open_id: str = "open-1",
    platform: str = "tiktok",
) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform=platform,
            external_account_id=open_id,
            handle="@northwindstudio",
            status="connected",
            granted_scopes_json=json.dumps(list(TIKTOK_SCOPES) if scopes is None else scopes),
            token_ref=None,
            metadata_json="{}",
        )
        session.add(account)
        session.flush()
        return account.id


def _scopes_of(url: str) -> list[str]:
    return parse_qs(urlparse(url).query)["scope"][0].split(",")


def _granted(database: Database, account_id: int) -> list[str]:
    with database.session() as session:
        row = session.get(ConnectedAccount, account_id)
        assert row is not None
        return json.loads(row.granted_scopes_json)  # type: ignore[no-any-return]


def test_enable_publishing_requests_video_upload_not_publish(
    client: TestClient, database: Database
) -> None:
    account_id = _account(database)
    response = client.post(f"/api/v1/accounts/{account_id}/publishing/enable")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "consent_required"
    scopes = _scopes_of(body["authorization_url"])
    assert "video.upload" in scopes
    # Not audited: video.publish is never asked for (spec 08 section 4).
    assert "video.publish" not in scopes
    assert "user.info.basic" in scopes
    assert body["state"]


def test_enable_publishing_adds_video_publish_only_when_audited(
    database: Database, settings: Settings, fake: FakeTikTok
) -> None:
    settings = _tiktok_settings(settings, tiktok_app_status="audited")
    registry = ProviderRegistry(database, settings)
    registry.set_provider(
        Platform.tiktok,
        TikTokProvider(settings, httpx.Client(transport=httpx.MockTransport(fake))),
    )
    app = FastAPI()
    app.state.settings = settings
    install_problem_handlers(app)
    app.include_router(build_accounts_router(database, settings, registry=registry))
    account_id = _account(database)
    with TestClient(app, base_url=BASE) as web:
        body = web.post(f"/api/v1/accounts/{account_id}/publishing/enable").json()
    scopes = _scopes_of(body["authorization_url"])
    assert "video.publish" in scopes
    assert "video.upload" in scopes


def test_enable_publishing_is_a_no_op_when_everything_is_granted(
    client: TestClient, database: Database
) -> None:
    account_id = _account(database, scopes=[*TIKTOK_SCOPES, "video.upload"])
    body = client.post(f"/api/v1/accounts/{account_id}/publishing/enable").json()
    assert body["status"] == "already_enabled"
    assert "authorization_url" not in body


def test_enable_publishing_refuses_other_platforms(client: TestClient, database: Database) -> None:
    account_id = _account(database, platform="youtube")
    response = client.post(f"/api/v1/accounts/{account_id}/publishing/enable")
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"


def test_enable_publishing_needs_accounts_manage(
    database: Database, settings: Settings, registry: ProviderRegistry
) -> None:
    settings = _tiktok_settings(settings)
    app = FastAPI()
    app.state.settings = settings
    install_problem_handlers(app)
    app.include_router(build_accounts_router(database, settings, registry=registry))
    app.dependency_overrides[get_principal] = lambda: Principal(
        kind="agent",
        id="key-1",
        workspace_id=1,
        role=None,
        scopes=frozenset({"accounts:read"}),
        display="Agent",
    )
    account_id = _account(database)
    with TestClient(app, base_url=BASE) as web:
        response = web.post(f"/api/v1/accounts/{account_id}/publishing/enable")
    assert response.status_code == 403
    assert response.json()["code"] == "scope_missing"


def test_enable_callback_stores_the_granted_scopes_on_the_same_account(
    client: TestClient, database: Database, registry: ProviderRegistry, fake: FakeTikTok
) -> None:
    account_id = _account(database)
    body = client.post(f"/api/v1/accounts/{account_id}/publishing/enable").json()
    callback = client.get(
        "/api/platforms/tiktok/oauth/callback",
        params={"code": "auth-code", "state": body["state"]},
        follow_redirects=False,
    )
    assert callback.status_code == 303, callback.text
    assert "error=" not in callback.headers["location"]
    assert _granted(database, account_id) == ["user.info.basic", "video.list", "video.upload"]
    # The new token set landed on the account's own ref.
    with database.session() as session:
        row = session.get(ConnectedAccount, account_id)
        assert row is not None and row.token_ref is not None
        stored = registry.token_store.load(row.token_ref)
    assert stored is not None and stored.access_token == "tt-access"


def test_enable_callback_refuses_a_different_tiktok_identity(
    database: Database, settings: Settings
) -> None:
    """Authorizing as another TikTok user must not bind scopes to this account."""
    settings = _tiktok_settings(settings)
    mismatched = FakeTikTok(open_id="someone-else")
    registry = ProviderRegistry(database, settings)
    registry.set_provider(
        Platform.tiktok,
        TikTokProvider(settings, httpx.Client(transport=httpx.MockTransport(mismatched))),
    )
    app = FastAPI()
    app.state.settings = settings
    install_problem_handlers(app)
    app.include_router(build_accounts_router(database, settings, registry=registry))
    app.include_router(build_platform_router(database, settings, registry=registry))
    account_id = _account(database, open_id="open-1")
    with TestClient(app, base_url=BASE) as web:
        body = web.post(f"/api/v1/accounts/{account_id}/publishing/enable").json()
        callback = web.get(
            "/api/platforms/tiktok/oauth/callback",
            params={"code": "auth-code", "state": body["state"]},
            follow_redirects=False,
        )
    assert callback.status_code == 303
    assert "error=account_mismatch" in callback.headers["location"]
    assert _granted(database, account_id) == list(TIKTOK_SCOPES)
    with database.session() as session:
        row = session.get(ConnectedAccount, account_id)
        assert row is not None and row.token_ref is None


def test_creator_info_is_fetched_fresh_on_every_call(
    client: TestClient, database: Database, registry: ProviderRegistry, fake: FakeTikTok
) -> None:
    account_id = _account(database)
    token_ref = registry.token_store.new_token_ref()
    registry.token_store.store(
        token_ref, TokenSet(access_token="tt-access", refresh_token="tt-refresh")
    )
    with database.session() as session:
        account = session.get(ConnectedAccount, account_id)
        assert account is not None
        account.token_ref = token_ref
    for _ in range(2):
        response = client.get(f"/api/v1/accounts/{account_id}/tiktok/creator-info")
        assert response.status_code == 200, response.text
    assert fake.creator_info_calls == 2
    body = response.json()
    assert body["creator_nickname"] == "Northwind Studio"
    assert body["privacy_level_options"] == [
        "PUBLIC_TO_EVERYONE",
        "MUTUAL_FOLLOW_FRIENDS",
        "SELF_ONLY",
    ]
    assert body["duet_disabled"] is True
    assert body["max_video_post_duration_sec"] == 300
    assert "Music Usage Confirmation" in body["consent"]["default"]
    assert "Branded Content Policy" in body["consent"]["branded"]


def test_creator_info_is_tiktok_only_and_scoped(
    database: Database, settings: Settings, registry: ProviderRegistry
) -> None:
    settings = _tiktok_settings(settings)
    app = FastAPI()
    app.state.settings = settings
    install_problem_handlers(app)
    app.include_router(build_accounts_router(database, settings, registry=registry))
    youtube = _account(database, platform="youtube")
    tiktok = _account(database)
    with TestClient(app, base_url=BASE) as web:
        other = web.get(f"/api/v1/accounts/{youtube}/tiktok/creator-info")
        missing = web.get(f"/api/v1/accounts/{tiktok}/tiktok/creator-info")
    assert other.status_code == 404
    # No token stored yet -> a visible "reconnect" answer, never a token in the response.
    assert missing.status_code == 422
    assert missing.json()["code"] == "validation_failed"
    assert "token" not in json.dumps(missing.json()).lower() or "reconnect" in missing.text


def test_enable_publishing_is_refused_in_mock_mode(database: Database, settings: Settings) -> None:
    settings = settings.model_copy(update={"platform_provider": "mock"})
    app = FastAPI()
    app.state.settings = settings
    install_problem_handlers(app)
    app.include_router(build_accounts_router(database, settings))
    account_id = _account(database)
    with TestClient(app, base_url=BASE) as web:
        response = web.post(f"/api/v1/accounts/{account_id}/publishing/enable")
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
