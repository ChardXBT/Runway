"""Instagram push gating (spec 04 section 2, RT3 item 2).

`POST /{ig-user-id}/subscribed_apps?subscribed_fields=comments,mentions` is
what turns an Instagram account's comment/mention webhooks on, but Meta only
accepts it once the app is Live with Advanced Access. So connect enqueues a
``subscribe_apps`` job only when ``instagram_app_status == "live_advanced"``;
below that the capability matrix honestly says polling and nothing is sent.

The provider is faked in tests (and the form is verified through
``httpx.MockTransport``); nothing touches the network.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from runway.api.app import create_app
from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import ConnectedAccount, PublishJob
from runway.jobs.queue import Job
from runway.jobs.runtime import build_worker
from runway.platforms.base import AccountIdentity, TokenSet
from runway.platforms.capabilities import Platform
from runway.platforms.instagram import GRAPH_BASE, InstagramProvider
from runway.platforms.registry import ProviderRegistry
from runway.realtime.subscriptions import SUBSCRIBE_APPS_JOB, run_subscribe_apps_job


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "platform_provider": "live",
        "instagram_app_id": "ig-app",
        "instagram_app_secret": "ig-secret",
    }
    base.update(overrides)
    return Settings(**base)


class _FakeInstagramProvider(InstagramProvider):
    """Real provider shell; the OAuth legs and the subscribe call are stubbed."""

    def __init__(self, settings: Settings) -> None:
        super().__init__(settings)
        self.subscribe_calls: list[tuple[str, str]] = []

    def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None,
    ) -> TokenSet:
        return TokenSet(
            access_token="ig-access",
            expires_at=datetime.now(UTC) + timedelta(days=60),
            scopes=["instagram_business_basic"],
            extra={"user_id": "ig-123"},
        )

    def fetch_identity(self, token: TokenSet) -> AccountIdentity:
        return AccountIdentity(
            external_account_id="ig-123",
            handle="@northwind",
            raw={"id": "ig-123"},
        )

    def subscribe_apps(self, token: TokenSet, *, ig_user_id: str) -> None:
        self.subscribe_calls.append((token.access_token, ig_user_id))


def _patch_instagram(monkeypatch: pytest.MonkeyPatch, provider: _FakeInstagramProvider) -> None:
    original = ProviderRegistry.provider

    def patched(self: ProviderRegistry, platform: Platform) -> Any:
        if platform is Platform.instagram:
            return provider
        return original(self, platform)

    monkeypatch.setattr(ProviderRegistry, "provider", patched)


def _connect_instagram(settings: Settings) -> None:
    """Drive the real connect + callback routes through a fake provider."""
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1:8000") as web:
        connect = web.post("/api/platforms/instagram/connect")
        assert connect.status_code == 200
        state = str(connect.json()["state"])
        callback = web.get(
            "/api/platforms/instagram/oauth/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 303, callback.text
        assert callback.headers["location"].endswith("/accounts?connected=instagram")
    app.state.database.engine.dispose()


def _jobs(database: Database, kind: str) -> list[PublishJob]:
    with database.session() as session:
        rows = session.scalars(
            select(PublishJob).where(PublishJob.kind == kind).order_by(PublishJob.id)
        ).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def _account(database: Database, *, token_ref: str | None = None) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform="instagram",
            external_account_id="ig-123",
            status="connected",
            granted_scopes_json="[]",
            token_ref=token_ref,
        )
        session.add(account)
        session.flush()
        return int(account.id)


# ---------------------------------------------------------------- gating


def test_a_live_advanced_connect_enqueues_subscribe_apps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, instagram_app_status="live_advanced")
    database = initialize_database(settings)
    _patch_instagram(monkeypatch, _FakeInstagramProvider(settings))
    try:
        _connect_instagram(settings)

        jobs = _jobs(database, SUBSCRIBE_APPS_JOB)
        assert len(jobs) == 1
        with database.session() as session:
            account_id = int(session.scalar(select(ConnectedAccount.id)) or 0)
        assert json.loads(jobs[0].payload_json) == {"account_id": account_id}
    finally:
        database.engine.dispose()


@pytest.mark.parametrize("status", ["development", "live_standard"])
def test_a_connect_below_live_advanced_enqueues_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    settings = _settings(tmp_path, instagram_app_status=status)
    database = initialize_database(settings)
    _patch_instagram(monkeypatch, _FakeInstagramProvider(settings))
    try:
        _connect_instagram(settings)

        assert _jobs(database, SUBSCRIBE_APPS_JOB) == []
    finally:
        database.engine.dispose()


def test_the_default_app_status_enqueues_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    assert settings.instagram_app_status == "development"
    database = initialize_database(settings)
    _patch_instagram(monkeypatch, _FakeInstagramProvider(settings))
    try:
        _connect_instagram(settings)

        assert _jobs(database, SUBSCRIBE_APPS_JOB) == []
    finally:
        database.engine.dispose()


# ---------------------------------------------------------------- the job


def test_the_job_subscribes_the_account_through_the_provider(tmp_path: Path) -> None:
    settings = _settings(tmp_path, instagram_app_status="live_advanced")
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    provider = _FakeInstagramProvider(settings)
    registry.set_provider(Platform.instagram, provider)
    try:
        token_ref = registry.token_store.new_token_ref()
        registry.token_store.store(
            token_ref,
            TokenSet(
                access_token="ig-access",
                expires_at=datetime.now(UTC) + timedelta(days=60),
            ),
        )
        account_id = _account(database, token_ref=token_ref)
        job = Job.of(
            PublishJob(
                id=1,
                kind=SUBSCRIBE_APPS_JOB,
                workspace_id=1,
                target_id=None,
                state="running",
                attempts=1,
                max_attempts=1,
                run_at=datetime.now(UTC),
                idempotency_key="test:subscribe_apps",
                payload_json=json.dumps({"account_id": account_id}),
                last_error=None,
            )
        )

        run_subscribe_apps_job(registry, job)

        assert provider.subscribe_calls == [("ig-access", "ig-123")]
    finally:
        database.engine.dispose()


def test_the_subscribed_apps_post_uses_the_documented_fields(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"success": True})

    provider = InstagramProvider(
        settings, httpx.Client(transport=httpx.MockTransport(handler), timeout=20.0)
    )
    provider.subscribe_apps(TokenSet(access_token="ig-access"), ig_user_id="ig-123")

    assert len(requests) == 1
    request = requests[0]
    assert request.method == "POST"
    assert request.url.path == f"{httpx.URL(GRAPH_BASE).path}/ig-123/subscribed_apps"
    assert request.url.params["subscribed_fields"] == "comments,mentions"
    assert request.headers["Authorization"] == "Bearer ig-access"


def test_a_disconnected_account_is_not_subscribed(tmp_path: Path) -> None:
    settings = _settings(tmp_path, instagram_app_status="live_advanced")
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    provider = _FakeInstagramProvider(settings)
    registry.set_provider(Platform.instagram, provider)
    try:
        with database.session() as session:
            account = ConnectedAccount(
                platform="instagram",
                external_account_id="ig-123",
                status="disconnected",
                granted_scopes_json="[]",
            )
            session.add(account)
            session.flush()
            account_id = int(account.id)
        job = Job.of(
            PublishJob(
                id=1,
                kind=SUBSCRIBE_APPS_JOB,
                workspace_id=1,
                target_id=None,
                state="running",
                attempts=1,
                max_attempts=1,
                run_at=datetime.now(UTC),
                idempotency_key="test:disconnected",
                payload_json=json.dumps({"account_id": account_id}),
                last_error=None,
            )
        )

        run_subscribe_apps_job(registry, job)

        assert provider.subscribe_calls == []
    finally:
        database.engine.dispose()


def test_subscribe_apps_is_registered_with_the_worker(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    try:
        worker = build_worker(settings, database)
        assert SUBSCRIBE_APPS_JOB in worker.handlers
    finally:
        database.engine.dispose()
