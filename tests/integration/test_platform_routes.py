from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from runway.api.app import create_app
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    AccountMetricSnapshot,
    CommentRecord,
    ConnectedAccount,
    PlatformContent,
)
from runway.platforms.capabilities import Platform
from runway.platforms.registry import ProviderRegistry
from runway.platforms.tokens import TokenStore
from runway.platforms.youtube import YouTubeProvider


@pytest.fixture()
def live_settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", platform_provider="live")


@pytest.fixture()
def mock_settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", platform_provider="mock")


def test_live_mode_without_credentials_reports_unconfigured(live_settings: Settings) -> None:
    with TestClient(create_app(live_settings), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/api/platforms")
        assert response.status_code == 200
        payload = response.json()
        assert payload["provider_mode"] == "live"
        entries = {entry["platform"]: entry for entry in payload["platforms"]}
        assert set(entries) == {"youtube", "instagram", "tiktok"}
        assert all(entry["configured"] is False for entry in entries.values())
        connect = client.post("/api/platforms/youtube/connect")
        assert connect.status_code == 409


def test_mock_mode_connect_is_rejected(mock_settings: Settings) -> None:
    with TestClient(create_app(mock_settings), base_url="http://127.0.0.1:8000") as client:
        response = client.post("/api/platforms/youtube/connect")
        assert response.status_code == 400


def _youtube_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/token":
        return httpx.Response(
            200,
            json={
                "access_token": "yt-access",
                "refresh_token": "yt-refresh",
                "expires_in": 3600,
                "scope": "https://www.googleapis.com/auth/youtube.readonly",
            },
        )
    if request.url.path == "/youtube/v3/channels":
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "UCtest",
                        "snippet": {
                            "title": "Northwind Studio",
                            "customUrl": "@northwindstudio",
                            "thumbnails": {"default": {"url": "https://img/a.jpg"}},
                        },
                        "statistics": {"subscriberCount": "10", "videoCount": "3"},
                        "contentDetails": {"relatedPlaylists": {"uploads": "UUt"}},
                    }
                ]
            },
        )
    return httpx.Response(404, json={})


def test_oauth_connect_and_callback_happy_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="live",
        youtube_client_id="yt-client",
        youtube_client_secret="yt-secret",
    )
    client = httpx.Client(transport=httpx.MockTransport(_youtube_handler), timeout=20.0)
    provider = YouTubeProvider(settings, client)
    original = ProviderRegistry.provider

    def patched(self: ProviderRegistry, platform: Platform):
        if platform is Platform.youtube:
            return provider
        return original(self, platform)

    monkeypatch.setattr(ProviderRegistry, "provider", patched)
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as web:
        connect = web.post("/api/platforms/youtube/connect")
        assert connect.status_code == 200
        payload = connect.json()
        url_query = parse_qs(urlparse(payload["authorization_url"]).query)
        assert url_query["state"] == [payload["state"]]
        assert url_query["code_challenge_method"] == ["S256"]
        assert url_query["code_challenge"]

        bad = web.get(
            "/api/platforms/youtube/oauth/callback",
            params={"code": "x", "state": "unknown-state"},
            follow_redirects=False,
        )
        assert bad.status_code == 303
        assert bad.headers["location"].endswith("/accounts?error=invalid_state")

        good = web.get(
            "/api/platforms/youtube/oauth/callback",
            params={"code": "auth-code", "state": payload["state"]},
            follow_redirects=False,
        )
        assert good.status_code == 303
        assert good.headers["location"].endswith("/accounts?connected=youtube")

        accounts = web.get("/api/platforms/accounts").json()
        assert len(accounts) == 1
        assert accounts[0]["platform"] == "youtube"
        assert accounts[0]["external_account_id"] == "UCtest"
        assert accounts[0]["status"] == "connected"


def test_mock_mode_auto_creates_three_accounts(mock_settings: Settings) -> None:
    app = create_app(mock_settings)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        accounts = client.get("/api/platforms/accounts").json()
        assert len(accounts) == 3
        # Mock seeding synchronously populated content, comments, and snapshots.
        database: Database = app.state.database
        with database.session() as session:
            assert (session.scalar(select(func.count()).select_from(PlatformContent)) or 0) > 0
            assert (session.scalar(select(func.count()).select_from(CommentRecord)) or 0) > 0
            assert (
                session.scalar(select(func.count()).select_from(AccountMetricSnapshot)) or 0
            ) >= 3
        assert {account["platform"] for account in accounts} == {
            "youtube",
            "instagram",
            "tiktok",
        }
        # Idempotent on repeat calls.
        again = client.get("/api/platforms/accounts").json()
        assert len(again) == 3
        # No token refs or token material may appear in API output.
        rendered = str(accounts).lower()
        assert "token_ref" not in rendered
        assert "mock-access-token" not in rendered
        assert "access_token" not in rendered


def test_disconnect_deletes_token_and_keeps_content(
    mock_settings: Settings,
) -> None:
    app = create_app(mock_settings)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        account = client.get("/api/platforms/accounts").json()[0]
        database: Database = app.state.database
        with database.session() as session:
            session.add(
                PlatformContent(
                    account_id=account["id"],
                    platform=account["platform"],
                    external_id="keep-me",
                    content_type="video",
                    title="kept",
                )
            )
        token_ref = None
        with database.session() as session:
            record = session.get(ConnectedAccount, account["id"])
            assert record is not None
            token_ref = record.token_ref
        assert token_ref is not None

        response = client.post(
            f"/api/platforms/accounts/{account['id']}/disconnect",
            json={"confirmed": True},
        )
        assert response.status_code == 200

        store = TokenStore(mock_settings.resolved_data_dir / "secrets")
        assert store.load(token_ref) is None
        with database.session() as session:
            record = session.get(ConnectedAccount, account["id"])
            assert record is not None
            assert record.status == "disconnected"
            assert record.token_ref is None
            kept = session.scalar(
                select(PlatformContent).where(
                    PlatformContent.account_id == account["id"],
                    PlatformContent.external_id == "keep-me",
                )
            )
            assert kept is not None


def test_disconnect_requires_confirmation(mock_settings: Settings) -> None:
    with TestClient(create_app(mock_settings), base_url="http://127.0.0.1:8000") as client:
        account = client.get("/api/platforms/accounts").json()[0]
        response = client.post(
            f"/api/platforms/accounts/{account['id']}/disconnect",
            json={"confirmed": False},
        )
        assert response.status_code == 422


def test_sync_endpoint_returns_202_and_run_completes(
    mock_settings: Settings,
) -> None:
    with TestClient(create_app(mock_settings), base_url="http://127.0.0.1:8000") as client:
        account = client.get("/api/platforms/accounts").json()[0]
        response = client.post(f"/api/platforms/accounts/{account['id']}/sync")
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        deadline = time.monotonic() + 15
        status = "running"
        while time.monotonic() < deadline:
            run = client.get(f"/api/platforms/sync-runs/{run_id}").json()
            status = run["status"]
            if status != "running":
                break
            time.sleep(0.1)
        assert status == "completed"
        runs = client.get(f"/api/platforms/accounts/{account['id']}/sync-runs").json()
        assert runs[0]["id"] == run_id


def test_local_request_guard_still_blocks_foreign_origin(
    mock_settings: Settings,
) -> None:
    with TestClient(create_app(mock_settings), base_url="http://127.0.0.1:8000") as client:
        response = client.post(
            "/api/platforms/accounts/1/disconnect",
            json={"confirmed": True},
            headers={"origin": "https://attacker.example"},
        )
        assert response.status_code == 403
        response = client.post(
            "/api/platforms/youtube/connect",
            headers={"origin": "https://attacker.example"},
        )
        assert response.status_code == 403
        # Safe reads are unaffected.
        assert client.get("/api/platforms").status_code == 200


def test_settings_endpoint_exposes_no_secret_values(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        youtube_client_secret="yt-secret-abc",
        instagram_app_secret="ig-secret-abc",
        tiktok_client_secret="tt-secret-abc",
    )
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        body = client.get("/api/settings").text
        for secret in ("yt-secret-abc", "ig-secret-abc", "tt-secret-abc"):
            assert secret not in body


def test_health_product_is_runway(mock_settings: Settings) -> None:
    with TestClient(create_app(mock_settings), base_url="http://127.0.0.1:8000") as client:
        payload = client.get("/health").json()
        assert payload["product"] == mock_settings.product_name
        assert payload["platform_provider"] == "mock"
