"""YouTube publishing consent: incremental OAuth, the wrong-channel guard and
granted-scope storage (spec 08 sections 1-2; youtube-a package 1).

`POST /api/v1/accounts/{id}/publishing/enable` starts an incremental consent
flow through the same state-row machinery as connect. Its callback must refuse
to attach a token whose channel differs from the account it was issued for,
and the granted scopes are stored after every exchange and every refresh (the
publishing capability is derived from them).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from runway.api.app import create_app
from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import ConnectedAccount, OAuthState
from runway.platforms.base import TokenSet
from runway.platforms.capabilities import (
    YOUTUBE_ANALYTICS_SCOPE,
    YOUTUBE_ENGAGE_SCOPE,
    YOUTUBE_READONLY_SCOPE,
    Platform,
)
from runway.platforms.publish_capability import (
    YOUTUBE_UPLOAD_SCOPE,
    AccountFacts,
    publishing_capability,
)
from runway.platforms.registry import ProviderRegistry
from runway.platforms.tokens import TokenStore
from runway.platforms.youtube import YouTubeProvider

READ_SCOPES = [YOUTUBE_READONLY_SCOPE, YOUTUBE_ANALYTICS_SCOPE]
UPLOAD_SCOPE = YOUTUBE_UPLOAD_SCOPE
ALL_SCOPES = [*READ_SCOPES, UPLOAD_SCOPE]


def migrated(settings: Settings) -> Settings:
    """The API never migrates; tests upgrade their temporary database first."""
    initialize_database(settings).engine.dispose()
    return settings


def live_settings(tmp_path: Path, **overrides: object) -> Settings:
    return migrated(
        Settings(
            data_dir=tmp_path / "data",
            platform_provider="live",
            youtube_client_id="yt-client",
            youtube_client_secret="yt-secret",
            **overrides,  # type: ignore[arg-type]
        )
    )


def add_youtube_account(
    database: Database,
    *,
    external_id: str = "UCexpected",
    scopes: list[str] | None = None,
    status: str = "connected",
    token_ref: str | None = "tok-1",
) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform="youtube",
            external_account_id=external_id,
            handle="@studio",
            display_name="Northwind Studio",
            status=status,
            granted_scopes_json=json.dumps(list(scopes if scopes is not None else READ_SCOPES)),
            token_ref=token_ref,
            metadata_json="{}",
        )
        session.add(account)
        session.flush()
        return account.id


def account_row(database: Database, account_id: int) -> ConnectedAccount:
    with database.session() as session:
        account = session.get(ConnectedAccount, account_id)
        assert account is not None
        session.expunge(account)
        return account


def make_google_handler(
    *,
    channel_id: str = "UCexpected",
    token_scope: str = " ".join(ALL_SCOPES),
    refresh_scope: str | None = None,
) -> tuple:
    """A MockTransport handler for the Google endpoints the flow touches."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            form = parse_qs(request.content.decode())
            if form.get("grant_type") == ["refresh_token"]:
                payload: dict[str, object] = {
                    "access_token": "yt-access-2",
                    "expires_in": 3600,
                }
                if refresh_scope is not None:
                    payload["scope"] = refresh_scope
                return httpx.Response(200, json=payload)
            return httpx.Response(
                200,
                json={
                    "access_token": "yt-access",
                    "refresh_token": "yt-refresh",
                    "expires_in": 3600,
                    "scope": token_scope,
                },
            )
        if request.url.path == "/youtube/v3/channels":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": channel_id,
                            "snippet": {
                                "title": "Northwind Studio",
                                "customUrl": "@northwindstudio",
                            },
                            "statistics": {"subscriberCount": "10"},
                            "contentDetails": {"relatedPlaylists": {"uploads": "UUt"}},
                        }
                    ]
                },
            )
        return httpx.Response(404, json={})

    return handler


def patched_provider(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, transport: httpx.MockTransport
) -> YouTubeProvider:
    provider = YouTubeProvider(settings, httpx.Client(transport=transport, timeout=20.0))
    original = ProviderRegistry.provider

    def patched(self: ProviderRegistry, platform: Platform):
        if platform is Platform.youtube:
            return provider
        return original(self, platform)

    monkeypatch.setattr(ProviderRegistry, "provider", patched)
    return provider


def _scope_param(authorization_url: str) -> set[str]:
    query = parse_qs(urlparse(authorization_url).query)
    return set(query["scope"][0].split())


def test_enable_publishing_issues_incremental_consent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    account_id = add_youtube_account(database)
    patched_provider(monkeypatch, settings, httpx.MockTransport(make_google_handler()))
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as web:
        response = web.post(f"/api/v1/accounts/{account_id}/publishing/enable")
        assert response.status_code == 200, response.text
        payload = response.json()
        query = parse_qs(urlparse(payload["authorization_url"]).query)
        # Incremental consent: the upload scope plus the read scopes the flow
        # re-requests so the grant cannot shrink below what Runway already uses.
        scopes = _scope_param(payload["authorization_url"])
        assert UPLOAD_SCOPE in scopes
        assert set(READ_SCOPES) <= scopes
        assert query["include_granted_scopes"] == ["true"]
        assert query["access_type"] == ["offline"]
        assert query["prompt"] == ["consent"]
        assert query["code_challenge"]
        assert UPLOAD_SCOPE in payload["requested_scopes"]
        with database.session() as session:
            record = session.scalar(select(OAuthState).where(OAuthState.state == payload["state"]))
            assert record is not None
            assert record.purpose == "enable_publishing"
            assert record.account_id == account_id
            assert record.workspace_id == 1


def test_enable_publishing_adds_the_posts_scope_when_the_pilot_is_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = live_settings(tmp_path, youtube_posts_api_access=True)
    database = Database(settings)
    account_id = add_youtube_account(database)
    patched_provider(monkeypatch, settings, httpx.MockTransport(make_google_handler()))
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as web:
        payload = web.post(f"/api/v1/accounts/{account_id}/publishing/enable").json()
        scopes = _scope_param(payload["authorization_url"])
        assert UPLOAD_SCOPE in scopes
        assert YOUTUBE_ENGAGE_SCOPE in scopes
        assert payload["requested_scopes"] == [UPLOAD_SCOPE, YOUTUBE_ENGAGE_SCOPE]


def test_enable_callback_stores_the_granted_scopes_and_the_capability_flips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    account_id = add_youtube_account(database)
    patched_provider(monkeypatch, settings, httpx.MockTransport(make_google_handler()))
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as web:
        payload = web.post(f"/api/v1/accounts/{account_id}/publishing/enable").json()
        callback = web.get(
            "/api/platforms/youtube/oauth/callback",
            params={"code": "auth-code", "state": payload["state"]},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"].endswith("/accounts?publishing_enabled=youtube")
    account = account_row(database, account_id)
    granted = set(json.loads(account.granted_scopes_json))
    assert UPLOAD_SCOPE in granted
    assert set(READ_SCOPES) <= granted
    assert account.status == "connected"
    # The capability is derived from the stored scopes, so it recomputes itself.
    facts = AccountFacts(status=account.status, granted_scopes=tuple(granted))
    capability = publishing_capability(
        "youtube_video",
        account=facts,
        app=settings_app_status(settings),
        profile="local",
        implemented={"youtube_direct"},
        media_host_available=False,
    )
    assert capability.state == "ready"
    # The new token (with the upload grant) is stored under the account's ref.
    token = TokenStore(settings.resolved_data_dir / "secrets").load(str(account.token_ref))
    assert token is not None and UPLOAD_SCOPE in token.scopes


def settings_app_status(settings: Settings):  # type: ignore[no-untyped-def]
    from runway.platforms.publish_capability import AppStatus

    return AppStatus.from_settings(settings)


def test_enable_callback_refuses_a_different_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    account_id = add_youtube_account(database)
    handler = make_google_handler(channel_id="UCother-channel")
    patched_provider(monkeypatch, settings, httpx.MockTransport(handler))
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as web:
        payload = web.post(f"/api/v1/accounts/{account_id}/publishing/enable").json()
        callback = web.get(
            "/api/platforms/youtube/oauth/callback",
            params={"code": "auth-code", "state": payload["state"]},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"].endswith("/accounts?error=wrong_channel")
    account = account_row(database, account_id)
    # The scope set and stored token of the other channel were never attached.
    assert set(json.loads(account.granted_scopes_json)) == set(READ_SCOPES)
    token = TokenStore(settings.resolved_data_dir / "secrets").load(str(account.token_ref))
    assert token is None or UPLOAD_SCOPE not in token.scopes


def test_reconnect_binds_the_state_and_refuses_a_different_channel(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    account_id = add_youtube_account(database, status="disconnected", token_ref=None)
    handler = make_google_handler(channel_id="UCother-channel")
    patched_provider(monkeypatch, settings, httpx.MockTransport(handler))
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as web:
        payload = web.post(f"/api/platforms/accounts/{account_id}/reconnect").json()
        with database.session() as session:
            record = session.scalar(select(OAuthState).where(OAuthState.state == payload["state"]))
            assert record is not None
            assert record.account_id == account_id
            assert record.purpose == "reconnect"
        callback = web.get(
            "/api/platforms/youtube/oauth/callback",
            params={"code": "auth-code", "state": payload["state"]},
            follow_redirects=False,
        )
        assert callback.status_code == 303
        assert callback.headers["location"].endswith("/accounts?error=wrong_channel")
        account = account_row(database, account_id)
        assert account.status == "disconnected"

        # A reconnect for the same channel attaches and reconnects it.
        patched_provider(monkeypatch, settings, httpx.MockTransport(make_google_handler()))
        payload = web.post(f"/api/platforms/accounts/{account_id}/reconnect").json()
        callback = web.get(
            "/api/platforms/youtube/oauth/callback",
            params={"code": "auth-code", "state": payload["state"]},
            follow_redirects=False,
        )
        assert callback.headers["location"].endswith("/accounts?connected=youtube")
        account = account_row(database, account_id)
        assert account.status == "connected"
        assert account.token_ref is not None


def test_a_refresh_stores_the_scopes_the_response_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    account_id = add_youtube_account(database)
    store = TokenStore(settings.resolved_data_dir / "secrets")
    store.store(
        "tok-1",
        TokenSet(
            access_token="old-access",
            refresh_token="refresh-1",
            expires_at=datetime.now(UTC) + timedelta(seconds=60),
            scopes=list(READ_SCOPES),
            extra={"channel_id": "UCexpected"},
        ),
    )
    handler = make_google_handler(refresh_scope=" ".join(ALL_SCOPES))
    patched_provider(monkeypatch, settings, httpx.MockTransport(handler))
    registry = ProviderRegistry(database, settings)
    token = registry.fresh_token(Platform.youtube, "tok-1")
    assert token.access_token == "yt-access-2"
    account = account_row(database, account_id)
    assert set(json.loads(account.granted_scopes_json)) == set(ALL_SCOPES)


def test_enable_publishing_is_refused_in_mock_mode(tmp_path: Path) -> None:
    settings = migrated(Settings(data_dir=tmp_path / "data", platform_provider="mock"))
    database = Database(settings)
    with database.session() as session:
        account = ConnectedAccount(
            platform="youtube",
            external_account_id="UC-mock",
            status="connected",
            granted_scopes_json="[]",
            metadata_json="{}",
        )
        session.add(account)
        session.flush()
        account_id = account.id
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as web:
        response = web.post(f"/api/v1/accounts/{account_id}/publishing/enable")
        assert response.status_code == 409
        assert response.json()["code"] == "capability_not_ready"


def test_enable_publishing_is_refused_for_other_platforms_and_unknown_accounts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    with database.session() as session:
        instagram = ConnectedAccount(
            platform="instagram",
            external_account_id="ig-1",
            status="connected",
            granted_scopes_json="[]",
            metadata_json="{}",
        )
        session.add(instagram)
        session.flush()
        instagram_id = instagram.id
    patched_provider(monkeypatch, settings, httpx.MockTransport(make_google_handler()))
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as web:
        response = web.post(f"/api/v1/accounts/{instagram_id}/publishing/enable")
        # Every platform has a branch now: this Instagram account reaches
        # Instagram's and fails its "OAuth configured" gate (no Meta
        # credentials are set in this environment).
        assert response.status_code == 409
        assert response.json()["code"] == "capability_not_ready"
        missing = web.post("/api/v1/accounts/9999/publishing/enable")
        assert missing.status_code == 404


def test_enable_publishing_needs_oauth_credentials(tmp_path: Path) -> None:
    settings = migrated(Settings(data_dir=tmp_path / "data", platform_provider="live"))
    database = Database(settings)
    account_id = add_youtube_account(database)
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as web:
        response = web.post(f"/api/v1/accounts/{account_id}/publishing/enable")
        assert response.status_code == 409
        assert response.json()["code"] == "capability_not_ready"
