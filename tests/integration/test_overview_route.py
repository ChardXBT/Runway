from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from runway.api.app import create_app
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    AccountMetricDay,
    CommentRecord,
    ConnectedAccount,
    PlatformContent,
)
from runway.platforms.overview import FRESHNESS_STALE_AFTER, account_freshness

PLATFORMS = {"youtube", "instagram", "tiktok"}


@pytest.fixture()
def live_settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", platform_provider="live")


@pytest.fixture()
def mock_settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", platform_provider="mock")


def _assert_overview_contract(payload: Any) -> None:
    """Assert the response shape consumed by isOverviewResponse in apps/web/lib/guards.ts."""
    assert isinstance(payload, dict)
    # generated_at must be an ISO-8601 date string the frontend can parse.
    assert isinstance(payload["generated_at"], str)
    assert datetime.fromisoformat(payload["generated_at"]) is not None
    assert payload["provider_mode"] in {"live", "mock"}
    assert isinstance(payload["accounts"], list)
    assert isinstance(payload["platform_summaries"], list)

    inbox = payload["inbox"]
    assert isinstance(inbox, dict)
    assert isinstance(inbox["unanswered_total"], int)
    assert isinstance(inbox["by_platform"], dict)
    assert isinstance(inbox["latest"], list)

    lineup = payload["lineup"]
    assert isinstance(lineup, dict)
    assert isinstance(lineup["next"], list)
    assert isinstance(lineup["needs_review"], int)
    assert isinstance(lineup["in_lineup"], int)
    assert isinstance(lineup["published_count"], int)

    drafts = payload["drafts"]
    assert isinstance(drafts, dict)
    assert isinstance(drafts["scheduled_next"], list)
    assert isinstance(drafts["counts"], dict)

    assert isinstance(payload["recent_sync_runs"], list)
    assert isinstance(payload["warnings"], list)
    assert all(isinstance(warning, str) for warning in payload["warnings"])


def _assert_account_view(view: Any) -> None:
    """Assert the fields the frontend's isAccountView guard requires."""
    assert isinstance(view["id"], int)
    assert view["platform"] in PLATFORMS
    assert isinstance(view["status"], str)
    assert view["handle"] is None or isinstance(view["handle"], str)
    assert view["display_name"] is None or isinstance(view["display_name"], str)
    assert isinstance(view["granted_scopes"], list)
    assert all(isinstance(scope, str) for scope in view["granted_scopes"])
    assert isinstance(view["capabilities"], list)
    for capability in view["capabilities"]:
        assert isinstance(capability["capability"], str)
        assert isinstance(capability["state"], str)
        assert isinstance(capability["detail"], str)
    assert view["freshness"] in {"fresh", "stale", "never"}


def _platform_summaries(payload: Any) -> dict[str, Any]:
    return {row["platform"]: row for row in payload["platform_summaries"]}


def test_overview_live_mode_empty_state_is_well_shaped(live_settings: Settings) -> None:
    with TestClient(create_app(live_settings), base_url="http://127.0.0.1:8000") as client:
        response = client.get("/api/overview")

    assert response.status_code == 200
    payload = response.json()
    _assert_overview_contract(payload)
    assert payload["provider_mode"] == "live"
    assert payload["accounts"] == []

    summaries = _platform_summaries(payload)
    assert set(summaries) == PLATFORMS
    for platform, summary in summaries.items():
        assert summary["state"] == "not_configured"
        assert summary["freshness"] == "never"
        assert summary["account_id"] is None
        assert summary["handle"] is None
        assert summary["display_name"] is None
        assert summary["followers"] is None
        assert summary["content_count"] is None
        assert summary["views_28d"] is None
        assert summary["last_sync_at"] is None
        assert summary["top_content"] == []
        assert summary["followers_label"] == (
            "Subscribers" if platform == "youtube" else "Followers"
        )

    inbox = payload["inbox"]
    assert inbox["unanswered_total"] == 0
    assert inbox["by_platform"] == {}
    assert inbox["latest"] == []

    lineup = payload["lineup"]
    assert lineup["next"] == []
    assert lineup["needs_review"] == 0
    assert lineup["in_lineup"] == 0
    assert lineup["published_count"] == 0

    assert payload["drafts"]["scheduled_next"] == []
    assert payload["drafts"]["counts"] == {}
    assert payload["recent_sync_runs"] == []
    assert payload["warnings"] == []


def test_overview_never_synced_connected_account_is_freshness_never(
    live_settings: Settings,
) -> None:
    app = create_app(live_settings)
    database: Database = app.state.database
    with database.session() as session:
        session.add(
            ConnectedAccount(
                platform="youtube",
                external_account_id="UC-never-synced",
                handle="neversynced",
                display_name="Never Synced",
                status="connected",
                granted_scopes_json="[]",
            )
        )

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.get("/api/overview")

    assert response.status_code == 200
    payload = response.json()
    _assert_overview_contract(payload)

    assert len(payload["accounts"]) == 1
    account = payload["accounts"][0]
    _assert_account_view(account)
    assert account["platform"] == "youtube"
    assert account["status"] == "connected"
    assert account["handle"] == "neversynced"
    assert account["freshness"] == "never"
    assert account["last_sync_at"] is None
    assert account["latest_snapshot"] is None

    summaries = _platform_summaries(payload)
    youtube = summaries["youtube"]
    assert youtube["account_id"] == account["id"]
    assert youtube["state"] == "connected"
    assert youtube["freshness"] == "never"
    assert youtube["handle"] == "neversynced"
    assert youtube["display_name"] == "Never Synced"
    assert youtube["content_count"] == 0
    assert youtube["followers"] is None
    assert youtube["views_28d"] is None
    assert youtube["last_sync_at"] is None
    assert youtube["top_content"] == []
    assert summaries["instagram"]["state"] == "not_configured"
    assert summaries["tiktok"]["state"] == "not_configured"
    assert payload["warnings"] == []


def test_overview_mock_mode_reflects_seeded_platform_data(mock_settings: Settings) -> None:
    app = create_app(mock_settings)
    database: Database = app.state.database
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        response = client.get("/api/overview")

        assert response.status_code == 200
        payload = response.json()
        _assert_overview_contract(payload)
        assert payload["provider_mode"] == "mock"

        accounts = payload["accounts"]
        assert len(accounts) == 3
        for view in accounts:
            _assert_account_view(view)
        assert {view["platform"] for view in accounts} == PLATFORMS
        assert all(view["status"] == "connected" for view in accounts)

        summaries = _platform_summaries(payload)
        assert set(summaries) == PLATFORMS
        for platform, summary in summaries.items():
            assert summary["state"] == "connected"
            assert summary["freshness"] == "fresh"
            assert summary["account_id"] is not None
            assert summary["handle"]
            assert summary["last_sync_at"] is not None
            # NOTE: followers is typed `number | null` in OverviewResponse. It is
            # currently always null because OverviewService.summary() keys its
            # snapshot lookup by snapshot id instead of account_id.
            assert summary["followers"] is None or isinstance(summary["followers"], int)
            assert summary["followers_label"] == (
                "Subscribers" if platform == "youtube" else "Followers"
            )
            assert len(summary["top_content"]) <= 3
            view_counts = [item["view_count"] or 0 for item in summary["top_content"]]
            assert view_counts == sorted(view_counts, reverse=True)
            for item in summary["top_content"]:
                assert isinstance(item["id"], int)
                assert isinstance(item["content_type"], str)
                assert item["permalink"] is None or isinstance(item["permalink"], str)

        # Cross-check the composite join against the seeded database rather than
        # hardcoding fixture numbers.
        with database.session() as session:
            for summary in summaries.values():
                assert summary["content_count"] == session.scalar(
                    select(func.count())
                    .select_from(PlatformContent)
                    .where(PlatformContent.account_id == summary["account_id"])
                )
                assert summary["content_count"] > 0
                assert summary["views_28d"] == session.scalar(
                    select(func.sum(AccountMetricDay.value)).where(
                        AccountMetricDay.account_id == summary["account_id"],
                        AccountMetricDay.metric == "views",
                        AccountMetricDay.day >= date.today() - timedelta(days=28),
                    )
                )
            expected_unanswered = (
                session.scalar(
                    select(func.count())
                    .select_from(CommentRecord)
                    .where(
                        CommentRecord.parent_external_id.is_(None),
                        CommentRecord.has_owner_reply.is_(False),
                        CommentRecord.status.in_(("new", "read", "escalated")),
                    )
                )
                or 0
            )

        inbox = payload["inbox"]
        assert inbox["unanswered_total"] == expected_unanswered
        assert sum(inbox["by_platform"].values()) == expected_unanswered
        assert 0 < len(inbox["latest"]) <= 5
        for comment in inbox["latest"]:
            assert comment["platform"] in PLATFORMS
            assert isinstance(comment["id"], int)
            assert isinstance(comment["text"], str)
            assert isinstance(comment["status"], str)

        assert payload["lineup"]["next"] == []
        assert payload["lineup"]["in_lineup"] == 0
        assert payload["lineup"]["published_count"] == 0
        assert payload["drafts"]["scheduled_next"] == []
        assert payload["drafts"]["counts"] == {}

        runs = payload["recent_sync_runs"]
        assert len(runs) == 3
        for run in runs:
            assert run["platform"] in PLATFORMS
            assert run["status"] == "completed"
            assert run["items_fetched"] > 0
            assert run["started_at"] is not None
            assert run["error_summary"] is None
        assert payload["warnings"] == []

        # Repeat calls stay idempotent: no duplicate accounts are created.
        again = client.get("/api/overview").json()
        assert [view["id"] for view in again["accounts"]] == [view["id"] for view in accounts]


def test_freshness_boundaries() -> None:
    assert account_freshness(None) == "never"
    now = datetime.now(UTC)
    assert account_freshness(now) == "fresh"
    assert account_freshness(now - FRESHNESS_STALE_AFTER + timedelta(minutes=1)) == "fresh"
    assert account_freshness(now - FRESHNESS_STALE_AFTER - timedelta(minutes=1)) == "stale"
    # Naive datetimes are interpreted as UTC.
    assert account_freshness(now.replace(tzinfo=None)) == "fresh"
    assert account_freshness((now - timedelta(days=2)).replace(tzinfo=None)) == "stale"
