"""Regressions found in the 2026-11-17 readiness audit of the platform routes."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from runway.api.app import create_app
from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import AccountMetricDay, ConnectedAccount
from runway.platforms.capabilities import Platform
from runway.platforms.overview import metric_window_start
from runway.platforms.registry import ProviderRegistry
from runway.platforms.tokens import TokenStore
from runway.platforms.youtube import YouTubeProvider

API = "http://127.0.0.1:8000"


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
                        "snippet": {"title": "Northwind Studio", "customUrl": "@northwind"},
                        "statistics": {"subscriberCount": "10", "videoCount": "3"},
                        "contentDetails": {"relatedPlaylists": {"uploads": "UUt"}},
                    }
                ]
            },
        )
    if request.url.path == "/revoke":
        return httpx.Response(200, json={})
    return httpx.Response(404, json={})


def _connect_via_oauth(web: TestClient) -> None:
    connect = web.post("/api/platforms/youtube/connect")
    assert connect.status_code == 200
    state = parse_qs(urlparse(connect.json()["authorization_url"]).query)["state"][0]
    callback = web.get(
        "/api/platforms/youtube/oauth/callback",
        params={"code": "auth-code", "state": state},
        follow_redirects=False,
    )
    assert callback.headers["location"].endswith("/accounts?connected=youtube")


def test_reconnect_after_disconnect_stores_a_usable_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="live",
        youtube_client_id="yt-client",
        youtube_client_secret="yt-secret",
    )
    provider = YouTubeProvider(
        settings, httpx.Client(transport=httpx.MockTransport(_youtube_handler))
    )
    original = ProviderRegistry.provider

    def patched(self: ProviderRegistry, platform: Platform):
        return provider if platform is Platform.youtube else original(self, platform)

    monkeypatch.setattr(ProviderRegistry, "provider", patched)
    initialize_database(settings).engine.dispose()
    app = create_app(settings)
    with TestClient(app, base_url=API) as web:
        _connect_via_oauth(web)
        account_id = web.get("/api/platforms/accounts").json()[0]["id"]
        disconnected = web.post(
            f"/api/platforms/accounts/{account_id}/disconnect", json={"confirmed": True}
        )
        assert disconnected.status_code == 200

        _connect_via_oauth(web)

        database: Database = app.state.database
        with database.session() as session:
            account = session.get(ConnectedAccount, account_id)
            assert account is not None
            assert account.status == "connected"
            # Before the fix the reconnect kept token_ref NULL and wrote the
            # token under the literal key "None", so the next sync failed.
            assert account.token_ref is not None
            assert account.token_ref != "None"
            token_ref = account.token_ref
    token = TokenStore(settings.resolved_data_dir / "secrets").load(token_ref)
    assert token is not None and token.access_token == "yt-access"


@pytest.fixture()
def mock_client(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", platform_provider="mock")
    initialize_database(settings).engine.dispose()
    app = create_app(settings)
    with TestClient(app, base_url=API) as client:
        yield app, client


def test_duplicate_draft_targets_are_a_validation_error_not_a_500(mock_client) -> None:
    _app, client = mock_client
    response = client.post(
        "/api/drafts",
        json={
            "title": "Two YouTube targets",
            "targets": [{"platform": "youtube"}, {"platform": "youtube"}],
        },
    )
    assert response.status_code == 422
    assert "once" in response.json()["detail"]
    assert client.get("/api/drafts").json() == []


def test_draft_schedule_keeps_the_instant_it_was_given(mock_client) -> None:
    _app, client = mock_client
    created = client.post(
        "/api/drafts",
        json={"title": "Offset draft", "scheduled_for": "2026-11-20T10:00:00+02:00"},
    )
    assert created.status_code == 200
    expected = datetime(2026, 11, 20, 8, 0, tzinfo=UTC)
    assert datetime.fromisoformat(created.json()["scheduled_for"]) == expected
    # Re-reading from the database must give the same instant, not 10:00 UTC.
    listed = client.get("/api/drafts").json()
    assert datetime.fromisoformat(listed[0]["scheduled_for"]) == expected

    updated = client.patch(
        f"/api/drafts/{created.json()['id']}",
        json={"scheduled_for": "2026-11-21T09:30:00-05:00"},
    )
    assert updated.status_code == 200
    listed = client.get("/api/drafts").json()
    assert datetime.fromisoformat(listed[0]["scheduled_for"]) == datetime(
        2026, 11, 21, 14, 30, tzinfo=UTC
    )


def test_metric_window_is_exactly_the_requested_number_of_days() -> None:
    today = date(2026, 11, 17)
    assert metric_window_start(7, today) == date(2026, 11, 11)
    assert (today - metric_window_start(28, today)).days + 1 == 28


@pytest.mark.parametrize("days", [7, 28, 90])
def test_analytics_range_counts_exactly_range_days(mock_client, days: int) -> None:
    app, client = mock_client
    account_id = client.get("/api/platforms/accounts").json()[0]["id"]
    database: Database = app.state.database
    today = datetime.now(UTC).date()
    with database.session() as session:
        session.execute(delete(AccountMetricDay).where(AccountMetricDay.account_id == account_id))
        for offset in range(max(2 * days, 28) + 5):
            session.add(
                AccountMetricDay(
                    account_id=account_id,
                    day=today - timedelta(days=offset),
                    metric="views",
                    value=1.0,
                    source="test",
                )
            )
    payload = client.get(f"/api/analytics/accounts/{account_id}", params={"range": days}).json()
    views = next(kpi for kpi in payload["kpis"] if kpi["key"] == "views")
    series = next(item for item in payload["series"] if item["metric"] == "views")
    # One view per day: the period total equals its length and the prior period
    # (also `days` long) is equal, so the delta is zero.
    assert len(series["points"]) == days
    assert views["value"] == days
    assert views["delta"] == 0

    overview = client.get("/api/overview").json()
    summary = next(
        item for item in overview["platform_summaries"] if item.get("account_id") == account_id
    )
    assert summary["views_28d"] == 28
