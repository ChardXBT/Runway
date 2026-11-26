from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from runway.api.app import create_app
from runway.auth.principal import Principal, get_principal, scopes_for_role
from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import (
    AccountMetricSnapshot,
    CommentRecord,
    ConnectedAccount,
    OAuthState,
    PlatformContent,
    Workspace,
)
from runway.operations import instance_lease
from runway.platforms.base import TokenSet
from runway.platforms.capabilities import Platform
from runway.platforms.registry import ProviderRegistry
from runway.platforms.tokens import TokenStore
from runway.platforms.youtube import YouTubeProvider


def migrated(settings: Settings) -> Settings:
    """The API never migrates; tests upgrade their temporary database first."""
    initialize_database(settings).engine.dispose()
    return settings


@pytest.fixture()
def live_settings(tmp_path: Path) -> Settings:
    return migrated(Settings(data_dir=tmp_path / "data", platform_provider="live"))


@pytest.fixture()
def mock_settings(tmp_path: Path) -> Settings:
    return migrated(Settings(data_dir=tmp_path / "data", platform_provider="mock"))


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
    with TestClient(create_app(migrated(settings)), base_url="http://127.0.0.1:8000") as web:
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


def test_oauth_connect_state_stays_in_the_callers_workspace(
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
    app = create_app(migrated(settings))
    database: Database = app.state.database
    with database.session() as session:
        session.add(Workspace(id=2, slug="other-workspace", name="Other workspace"))
    caller = Principal(
        kind="user",
        id="user-2",
        workspace_id=2,
        role="owner",
        scopes=scopes_for_role("owner"),
        display="Other owner",
    )
    app.dependency_overrides[get_principal] = lambda: caller
    with TestClient(app, base_url="http://127.0.0.1:8000") as web:
        connect = web.post("/api/platforms/youtube/connect")
        assert connect.status_code == 200, connect.text
    with database.session() as session:
        state = session.scalar(select(OAuthState))
        assert state is not None
        assert state.workspace_id == 2


def test_unified_connect_state_stays_in_the_callers_workspace(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        meta_app_id="meta-app",
        meta_app_secret="meta-secret",
        instagram_unified_config_id="cfg-12345",
    )
    app = create_app(migrated(settings))
    database: Database = app.state.database
    with database.session() as session:
        session.add(Workspace(id=2, slug="other-workspace", name="Other workspace"))
    caller = Principal(
        kind="user",
        id="user-2",
        workspace_id=2,
        role="owner",
        scopes=scopes_for_role("owner"),
        display="Other owner",
    )
    app.dependency_overrides[get_principal] = lambda: caller
    with TestClient(app, base_url="http://127.0.0.1:8000") as web:
        connect = web.post(
            "/api/platforms/instagram/connect", json={"adapter": "instagram_unified"}
        )
        assert connect.status_code == 200, connect.text
    with database.session() as session:
        state = session.scalar(select(OAuthState))
        assert state is not None
        assert state.workspace_id == 2


def test_oauth_callback_rejects_state_from_another_workspace(
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
    app = create_app(migrated(settings))
    database: Database = app.state.database
    with database.session() as session:
        session.add(Workspace(id=2, slug="other-workspace", name="Other workspace"))

    def principal(workspace_id: int) -> Principal:
        return Principal(
            kind="user",
            id=f"user-{workspace_id}",
            workspace_id=workspace_id,
            role="owner",
            scopes=scopes_for_role("owner"),
            display=f"Owner {workspace_id}",
        )

    app.dependency_overrides[get_principal] = lambda: principal(2)
    with TestClient(app, base_url="http://127.0.0.1:8000") as web:
        connect = web.post("/api/platforms/youtube/connect")
        assert connect.status_code == 200, connect.text
        state_value = connect.json()["state"]
        app.dependency_overrides[get_principal] = lambda: principal(1)
        callback = web.get(
            "/api/platforms/youtube/oauth/callback",
            params={"code": "auth-code", "state": state_value},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"].endswith("/accounts?error=invalid_state")
    with database.session() as session:
        state = session.scalar(select(OAuthState).where(OAuthState.state == state_value))
        assert state is not None
        assert state.consumed_at is None
        assert session.scalar(select(ConnectedAccount)) is None


def test_oauth_callback_gates_standby_before_exchanging_the_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir()
    settings = Settings(
        data_dir=tmp_path / "data",
        instance_dir=instance_dir,
        machine_id="this-machine",
        token_key=Fernet.generate_key().decode("ascii"),
        platform_provider="live",
        youtube_client_id="yt-client",
        youtube_client_secret="yt-secret",
    )
    # This machine holds the lease and confirmed it remotely just now: active.
    heartbeat = datetime.now(UTC)
    instance_lease.write_lease(
        instance_dir,
        instance_lease.Lease(holder="this-machine", holder_since=heartbeat, heartbeat_at=heartbeat),
    )
    instance_lease.record_remote_check(instance_dir, "this-machine", heartbeat)
    client = httpx.Client(transport=httpx.MockTransport(_youtube_handler), timeout=20.0)
    provider = YouTubeProvider(settings, client)
    exchanged: list[str] = []
    real_exchange = provider.exchange_code

    def exchange_spy(*, code: str, redirect_uri: str, code_verifier: str | None) -> TokenSet:
        exchanged.append(code)
        return real_exchange(code=code, redirect_uri=redirect_uri, code_verifier=code_verifier)

    monkeypatch.setattr(provider, "exchange_code", exchange_spy)
    original = ProviderRegistry.provider

    def patched(self: ProviderRegistry, platform: Platform):
        if platform is Platform.youtube:
            return provider
        return original(self, platform)

    monkeypatch.setattr(ProviderRegistry, "provider", patched)
    initialize_database(settings).engine.dispose()
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as web:
        connect = web.post("/api/platforms/youtube/connect")
        assert connect.status_code == 200
        state = parse_qs(urlparse(connect.json()["authorization_url"]).query)["state"][0]

        # A push or handover on this machine makes it standby between the idle
        # wait and the lease release.
        with instance_lease.instance_transfer(settings, "push"):
            standby = web.get(
                "/api/platforms/youtube/oauth/callback",
                params={"code": "auth-code", "state": state},
                follow_redirects=False,
            )

        assert standby.status_code == 303
        assert standby.headers["location"].endswith("/accounts?error=standby")
        # The single-use authorization code was never burned at the provider.
        assert exchanged == []

        # The state was not consumed: the same callback succeeds once active.
        good = web.get(
            "/api/platforms/youtube/oauth/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert good.status_code == 303
        assert good.headers["location"].endswith("/accounts?connected=youtube")
        assert exchanged == ["auth-code"]


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


def test_sync_of_disconnected_account_is_refused_and_status_kept(
    mock_settings: Settings,
) -> None:
    app = create_app(mock_settings)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        account = client.get("/api/platforms/accounts").json()[0]
        disconnect = client.post(
            f"/api/platforms/accounts/{account['id']}/disconnect",
            json={"confirmed": True},
        )
        assert disconnect.status_code == 200
        runs_before = client.get(f"/api/platforms/accounts/{account['id']}/sync-runs").json()

        response = client.post(f"/api/platforms/accounts/{account['id']}/sync")

        assert response.status_code == 409
        assert "reconnect" in response.json()["detail"]
        database: Database = app.state.database
        with database.session() as session:
            record = session.get(ConnectedAccount, account["id"])
            assert record is not None
            assert record.status == "disconnected"
            assert record.last_error is None
        runs_after = client.get(f"/api/platforms/accounts/{account['id']}/sync-runs").json()
        assert runs_after == runs_before


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
    with TestClient(create_app(migrated(settings)), base_url="http://127.0.0.1:8000") as client:
        body = client.get("/api/settings").text
        for secret in ("yt-secret-abc", "ig-secret-abc", "tt-secret-abc"):
            assert secret not in body


def test_health_product_is_runway(mock_settings: Settings) -> None:
    with TestClient(create_app(mock_settings), base_url="http://127.0.0.1:8000") as client:
        payload = client.get("/health").json()
        assert payload["product"] == mock_settings.product_name
        assert payload["platform_provider"] == "mock"
