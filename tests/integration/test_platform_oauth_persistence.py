from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runway.api.app import create_app
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ConnectedAccount
from runway.platforms.base import TokenSet
from runway.platforms.capabilities import Platform
from runway.platforms.registry import ProviderRegistry
from runway.platforms.sync import SyncService
from runway.platforms.tokens import TokenStore, TokenStoreError
from runway.platforms.youtube import YouTubeProvider


@pytest.fixture()
def oauth_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="live",
        youtube_client_id="yt-client",
        youtube_client_secret="yt-secret",
    )
    provider_state = {
        "access_token": "access-one",
        "refresh_token": "refresh-one",
        "expires_in": 3600,
        "external_account_id": "UCtest",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/token":
            form = parse_qs(request.content.decode("utf-8"))
            if form.get("grant_type") == ["refresh_token"]:
                return httpx.Response(
                    200,
                    json={"access_token": "access-refreshed", "expires_in": 3600},
                )
            return httpx.Response(
                200,
                json={
                    "access_token": provider_state["access_token"],
                    "refresh_token": provider_state["refresh_token"],
                    "expires_in": provider_state["expires_in"],
                    "scope": "https://www.googleapis.com/auth/youtube.readonly",
                },
            )
        if request.url.path == "/revoke":
            return httpx.Response(200, json={})
        if request.url.path == "/youtube/v3/channels":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": provider_state["external_account_id"],
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

    http_client = httpx.Client(transport=httpx.MockTransport(handler), timeout=20.0)
    provider = YouTubeProvider(settings, http_client)
    original_provider = ProviderRegistry.provider

    def patched_provider(self: ProviderRegistry, platform: Platform):
        if platform is Platform.youtube:
            return provider
        return original_provider(self, platform)

    monkeypatch.setattr(ProviderRegistry, "provider", patched_provider)
    app = create_app(settings)
    yield SimpleNamespace(
        app=app,
        settings=settings,
        database=app.state.database,
        provider_state=provider_state,
    )
    http_client.close()
    app.state.database.engine.dispose()


def _complete_connect(web: TestClient) -> httpx.Response:
    connect = web.post("/api/platforms/youtube/connect")
    assert connect.status_code == 200
    state = connect.json()["state"]
    return web.get(
        "/api/platforms/youtube/oauth/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )


def _complete_reconnect(web: TestClient, account_id: int) -> httpx.Response:
    connect = web.post(f"/api/platforms/accounts/{account_id}/reconnect")
    assert connect.status_code == 200
    state = connect.json()["state"]
    return web.get(
        "/api/platforms/youtube/oauth/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )


def _single_account(database: Database) -> ConnectedAccount:
    with database.session() as session:
        account = session.scalar(select(ConnectedAccount))
        assert account is not None
        session.expunge(account)
        return account


def _insert_connected_account(database: Database, token_ref: str) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform="youtube",
            external_account_id="UCpartial",
            status="connected",
            token_ref=token_ref,
            granted_scopes_json="[]",
        )
        session.add(account)
        session.flush()
        return account.id


def test_interrupted_token_write_does_not_create_a_connected_account(
    oauth_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_store(self: TokenStore, token_ref: str, token: TokenSet) -> None:
        raise TokenStoreError("simulated token-write interruption")

    monkeypatch.setattr(TokenStore, "store", fail_store)
    with TestClient(oauth_env.app, base_url="http://127.0.0.1:8000") as web:
        response = _complete_connect(web)

        assert response.status_code == 303
        assert response.headers["location"].endswith("/accounts?error=token_storage_failed")
        with oauth_env.database.session() as session:
            count = session.scalar(select(func.count()).select_from(ConnectedAccount))
        assert count == 0


def test_missing_token_is_reconciled_to_needs_reauth(oauth_env) -> None:
    _insert_connected_account(oauth_env.database, "missing-token-ref")

    with TestClient(oauth_env.app, base_url="http://127.0.0.1:8000") as web:
        response = web.get("/api/platforms/accounts")

    assert response.status_code == 200
    account = response.json()[0]
    assert account["status"] == "needs_reauth"
    assert "reconnect" in account["last_error"]
    assert all(not capability["ready"] for capability in account["capabilities"])


def test_undecryptable_token_blob_is_reconciled_without_exposing_token_material(oauth_env) -> None:
    token_ref = "corrupt-token-ref"
    _insert_connected_account(oauth_env.database, token_ref)
    store = TokenStore(oauth_env.settings.resolved_data_dir / "secrets")
    store.store(token_ref, TokenSet(access_token="fixture-access-token"))
    blob = bytearray(store.store_path.read_bytes())
    blob[len(blob) // 2] ^= 0xFF
    store.store_path.write_bytes(bytes(blob))

    with TestClient(oauth_env.app, base_url="http://127.0.0.1:8000") as web:
        response = web.get("/api/platforms/accounts")

    assert response.status_code == 200
    assert "fixture-access-token" not in response.text
    account = response.json()[0]
    assert account["status"] == "needs_reauth"
    assert "reconnect" in account["last_error"]


def test_reconnect_replaces_token_without_duplicating_the_account(oauth_env) -> None:
    with TestClient(oauth_env.app, base_url="http://127.0.0.1:8000") as web:
        first = _complete_connect(web)
        assert first.status_code == 303
        first_account = _single_account(oauth_env.database)
        first_ref = str(first_account.token_ref)

        oauth_env.provider_state["access_token"] = "access-two"
        oauth_env.provider_state["refresh_token"] = "refresh-two"
        second = _complete_reconnect(web, first_account.id)

        assert second.status_code == 303
        assert second.headers["location"].endswith("/accounts?connected=youtube")
        with oauth_env.database.session() as session:
            accounts = session.scalars(select(ConnectedAccount)).all()
        assert len(accounts) == 1
        assert accounts[0].external_account_id == "UCtest"
        assert accounts[0].status == "connected"
        assert accounts[0].token_ref != first_ref

        store = TokenStore(oauth_env.settings.resolved_data_dir / "secrets")
        replacement = store.load(str(accounts[0].token_ref))
        assert replacement is not None
        assert replacement.access_token == "access-two"
        assert replacement.refresh_token == "refresh-two"
        assert store.load(first_ref) is None


def test_crash_after_replacement_write_before_account_commit_keeps_old_token_usable(
    oauth_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    with TestClient(oauth_env.app, base_url="http://127.0.0.1:8000") as web:
        first = _complete_connect(web)
        assert first.status_code == 303
        first_account = _single_account(oauth_env.database)
        first_ref = str(first_account.token_ref)
        oauth_env.provider_state["access_token"] = "access-two"

        original_commit = Session.commit

        def crash_on_connected_account_commit(session: Session) -> None:
            if any(isinstance(item, ConnectedAccount) for item in session.dirty):
                raise RuntimeError("simulated crash before account commit")
            return original_commit(session)

        monkeypatch.setattr(Session, "commit", crash_on_connected_account_commit)
        response = _complete_reconnect(web, first_account.id)
        monkeypatch.setattr(Session, "commit", original_commit)

        assert response.status_code == 303
        account = _single_account(oauth_env.database)
        assert account.status == "connected"
        assert account.token_ref == first_ref
        store = TokenStore(oauth_env.settings.resolved_data_dir / "secrets")
        old_token = store.load(first_ref)
        assert old_token is not None
        assert old_token.access_token == "access-one"


def test_reconciliation_removes_unreferenced_token_from_interrupted_connect(oauth_env) -> None:
    database = oauth_env.database
    live_ref = "live-token-ref"
    orphan_ref = "orphaned-token-ref"
    _insert_connected_account(database, live_ref)
    store = TokenStore(oauth_env.settings.resolved_data_dir / "secrets")
    store.store(live_ref, TokenSet(access_token="live-fixture-token"))
    store.store(orphan_ref, TokenSet(access_token="orphan-fixture-token"))

    result = ProviderRegistry(database, oauth_env.settings).reconcile_connected_accounts()

    assert result.orphaned_tokens_removed == 1
    assert store.load(live_ref) is not None
    assert store.load(orphan_ref) is None


def test_concurrent_reconnect_and_disconnect_never_reports_connected_without_token(oauth_env) -> None:
    with TestClient(oauth_env.app, base_url="http://127.0.0.1:8000") as web:
        first = _complete_connect(web)
        assert first.status_code == 303
        account = _single_account(oauth_env.database)
        connect = web.post(f"/api/platforms/accounts/{account.id}/reconnect")
        assert connect.status_code == 200
        state = connect.json()["state"]
        barrier = threading.Barrier(2)
        outcomes: list[tuple[str, int]] = []

        def run_callback() -> None:
            barrier.wait()
            response = web.get(
                "/api/platforms/youtube/oauth/callback",
                params={"code": "auth-code", "state": state},
                follow_redirects=False,
            )
            outcomes.append(("connect", response.status_code))

        def run_disconnect() -> None:
            barrier.wait()
            response = web.post(
                f"/api/platforms/accounts/{account.id}/disconnect",
                json={"confirmed": True},
            )
            outcomes.append(("disconnect", response.status_code))

        threads = [threading.Thread(target=run_callback), threading.Thread(target=run_disconnect)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(outcomes) == [("connect", 303), ("disconnect", 200)]

        final_account = _single_account(oauth_env.database)
        store = TokenStore(oauth_env.settings.resolved_data_dir / "secrets")
        if final_account.status == "connected":
            assert final_account.token_ref is not None
            assert store.load(str(final_account.token_ref)) is not None
        else:
            assert final_account.status == "disconnected"
            assert final_account.token_ref is None


def test_expired_access_token_is_refreshed_instead_of_requiring_reauth(oauth_env) -> None:
    oauth_env.provider_state["expires_in"] = 0
    with TestClient(oauth_env.app, base_url="http://127.0.0.1:8000") as web:
        response = _complete_connect(web)
        assert response.status_code == 303

    account = _single_account(oauth_env.database)
    registry = ProviderRegistry(oauth_env.database, oauth_env.settings)
    fresh = SyncService(oauth_env.database, oauth_env.settings, registry).ensure_fresh(account.id)

    assert fresh.access_token == "access-refreshed"
    assert _single_account(oauth_env.database).status == "connected"
