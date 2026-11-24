"""`GET /api/v1/accounts/{id}/sync-status` (spec 04 section 5, RT2 part D).

The Accounts page reads every scope's freshness from this route: the last
outcome, the next due time, whether a push subscription is live, today's
provider quota use and whether the adaptive scheduler is on. Each scope run
also leaves a `sync.started`/`sync.finished` pair on the event log.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from runway.api.app import create_app
from runway.auth.principal import Principal, get_principal
from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import (
    ConnectedAccount,
    EventRecord,
    PlatformSubscription,
    Workspace,
)
from runway.platforms.registry import ProviderRegistry
from runway.platforms.sync import SyncService
from runway.realtime.quota import record_youtube_call
from runway.realtime.schedule import SCOPES, boost, record_scope_run

START = datetime(2026, 11, 23, 20, 0, tzinfo=UTC)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _app(tmp_path: Path, **overrides: object) -> tuple[Settings, Database, FastAPI, TestClient]:
    settings = Settings(data_dir=tmp_path / "data", platform_provider="mock", **overrides)
    database = initialize_database(settings)
    application = create_app(settings)
    return (
        settings,
        database,
        application,
        TestClient(application, base_url="http://127.0.0.1:8000"),
    )


def _account(
    database: Database,
    platform: str = "youtube",
    external: str = "ext-1",
    workspace_id: int = 1,
) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform=platform,
            external_account_id=external,
            status="connected",
            workspace_id=workspace_id,
            granted_scopes_json="[]",
        )
        session.add(account)
        session.flush()
        return int(account.id)


def _events(database: Database, account_id: int, type: str) -> list[EventRecord]:  # noqa: A002
    with database.session() as session:
        rows = session.scalars(
            select(EventRecord).where(
                EventRecord.type == type,
                EventRecord.subject_id == str(account_id),
            )
        ).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


# ---------------------------------------------------------------- the route


def test_sync_status_returns_every_scope(tmp_path: Path) -> None:
    _settings, database, _app_obj, client = _app(tmp_path)
    account_id = _account(database)
    # An active workspace: the 14-day inactivity floor stays off the cadence.
    with database.session() as session:
        workspace = session.get(Workspace, 1)
        assert workspace is not None
        workspace.last_seen_at = START
    record_scope_run(
        database, account_id, "inventory", status="completed", now=START, rand=lambda: 0.5
    )
    record_scope_run(
        database,
        account_id,
        "comments",
        status="failed",
        error="rate limited",
        now=START,
        rand=lambda: 0.5,
    )

    response = client.get(f"/api/v1/accounts/{account_id}/sync-status")
    assert response.status_code == 200
    body = response.json()
    assert body["account_id"] == account_id
    assert body["adaptive_sync_enabled"] is True
    assert body["push_enabled"] is False
    assert body["quota"] == {"units_used": 0, "budget": 10000, "reserve": 3000}

    scopes = {entry["scope"]: entry for entry in body["scopes"]}
    assert set(scopes) == set(SCOPES)

    inventory = scopes["inventory"]
    assert inventory["last_status"] == "completed"
    assert _aware(datetime.fromisoformat(inventory["last_run_at"])) == START
    assert _aware(datetime.fromisoformat(inventory["next_run_at"])) == (
        START + timedelta(minutes=30)
    )
    assert inventory["boosted_until"] is None
    assert inventory["source"] == "poll"

    comments = scopes["comments"]
    assert comments["last_status"] == "failed"

    # A scope that never ran reports nulls rather than pretending freshness.
    identity = scopes["identity"]
    assert identity["last_run_at"] is None
    assert identity["last_status"] is None
    assert identity["source"] == "poll"


def test_push_enabled_and_a_boosted_scope_show_through(tmp_path: Path) -> None:
    _settings, database, _app_obj, client = _app(tmp_path)
    account_id = _account(database)
    with database.session() as session:
        session.add(
            PlatformSubscription(
                account_id=account_id,
                provider="youtube",
                topic="https://www.youtube.com/xml/feeds/videos.xml?channel_id=UC-x",
                state="active",
            )
        )
        # A pending subscription does not count.
        session.add(
            PlatformSubscription(
                account_id=account_id,
                provider="youtube",
                topic="https://example.com/stale",
                state="pending",
            )
        )
    boost(database, account_id, "inventory", 30, now=START)

    body = client.get(f"/api/v1/accounts/{account_id}/sync-status").json()
    assert body["push_enabled"] is True
    inventory = {entry["scope"]: entry for entry in body["scopes"]}["inventory"]
    assert inventory["source"] == "push"
    assert _aware(datetime.fromisoformat(inventory["boosted_until"])) == (
        START + timedelta(minutes=30)
    )


def test_quota_use_reflects_todays_recorded_units(tmp_path: Path) -> None:
    settings, database, _app_obj, client = _app(tmp_path)
    account_id = _account(database)
    with database.session() as session:
        record_youtube_call(session, "videos.list")
        record_youtube_call(session, "comments.insert")

    body = client.get(f"/api/v1/accounts/{account_id}/sync-status").json()
    assert body["quota"]["units_used"] == 51
    # A different reserve percent is visible to the page.
    assert body["quota"]["reserve"] == 3000
    assert settings.youtube_quota_publish_reserve_percent == 30


def test_non_youtube_accounts_have_no_unit_budget(tmp_path: Path) -> None:
    _settings, database, _app_obj, client = _app(tmp_path)
    account_id = _account(database, platform="instagram")
    body = client.get(f"/api/v1/accounts/{account_id}/sync-status").json()
    assert body["quota"]["units_used"] == 0
    assert body["quota"]["budget"] is None
    assert body["quota"]["reserve"] is None


def test_an_unknown_or_foreign_account_is_a_404(tmp_path: Path) -> None:
    _settings, database, _app_obj, client = _app(tmp_path)
    assert client.get("/api/v1/accounts/98765/sync-status").status_code == 404

    with database.session() as session:
        session.add(Workspace(slug="other", name="Other workspace"))
    foreign_id = _account(database, external="ext-2", workspace_id=2)
    response = client.get(f"/api/v1/accounts/{foreign_id}/sync-status")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


def test_sync_status_requires_accounts_read(tmp_path: Path) -> None:
    _settings, database, application, client = _app(tmp_path)
    account_id = _account(database)
    restricted = Principal(
        kind="agent",
        id="agent-1",
        workspace_id=1,
        role=None,
        scopes=frozenset({"posts:read"}),
        display="agent",
    )
    application.dependency_overrides[get_principal] = lambda: restricted
    response = client.get(f"/api/v1/accounts/{account_id}/sync-status")
    assert response.status_code == 403
    assert response.json()["code"] == "scope_missing"


# --------------------------------------------------------- per-scope events


@pytest.fixture()
def sync_env(tmp_path: Path):
    settings = Settings(data_dir=tmp_path / "data", platform_provider="mock")
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    registry.ensure_mock_accounts()
    service = SyncService(database, settings, registry)
    yield settings, database, registry, service
    database.engine.dispose()


def test_a_scope_run_emits_started_and_finished_events(sync_env) -> None:
    _settings, database, _registry, service = sync_env
    with database.session() as session:
        account = session.scalar(
            select(ConnectedAccount).where(ConnectedAccount.platform == "youtube")
        )
        assert account is not None
        account_id = int(account.id)

    run = service.run(account_id, scopes=["inventory"])

    # The mock seed ran earlier scopes too; this run's pair is keyed by run_id.
    started_payloads = [
        json.loads(row.payload_json) for row in _events(database, account_id, "sync.started")
    ]
    finished_payloads = [
        json.loads(row.payload_json) for row in _events(database, account_id, "sync.finished")
    ]
    assert [p["scope"] for p in started_payloads if p["run_id"] == run.id] == ["inventory"]
    this_run = [p for p in finished_payloads if p["run_id"] == run.id]
    assert [payload["scope"] for payload in this_run] == ["inventory"]
    assert this_run[0]["status"] == "completed"


def test_a_full_sync_emits_one_pair_per_covered_scope(sync_env) -> None:
    _settings, database, _registry, service = sync_env
    with database.session() as session:
        account = session.scalar(
            select(ConnectedAccount).where(ConnectedAccount.platform == "youtube")
        )
        assert account is not None
        account_id = int(account.id)

    run = service.run(account_id)

    started_scopes = {
        json.loads(row.payload_json)["scope"]
        for row in _events(database, account_id, "sync.started")
        if json.loads(row.payload_json)["run_id"] == run.id
    }
    finished_scopes = {
        json.loads(row.payload_json)["scope"]
        for row in _events(database, account_id, "sync.finished")
        if json.loads(row.payload_json)["run_id"] == run.id
    }
    assert (
        started_scopes
        == finished_scopes
        == {
            "identity",
            "inventory",
            "content_metrics_recent",
            "daily_metrics",
            "comments",
        }
    )
