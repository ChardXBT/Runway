"""Per-workspace quotas, the workspace kill switch and the global publishing pause."""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from runway.api.platform_routes import build_platform_router
from runway.auth.principal import Principal, get_principal, local_owner, scopes_for_role
from runway.auth.problems import Problem, problem_handler
from runway.cli import hosted_commands
from runway.cli.main import app as cli
from runway.config import Settings
from runway.db import Database
from runway.db.models import ConnectedAccount, Workspace
from runway.hosted.quotas import (
    UNLIMITED,
    PublishingPaused,
    WorkspaceQuotas,
    publishing_allowed,
    publishing_gate,
    quotas_for,
    require_connected_account_slot,
    require_send_allowed,
    set_publishing_disabled,
    set_quota_overrides,
)


def _hosted(settings: Settings, **overrides: object) -> Settings:
    return settings.model_copy(update={"profile": "hosted", **overrides})


def test_local_profile_is_unlimited_even_with_overrides(
    settings: Settings, database: Database
) -> None:
    with database.session() as session:
        set_quota_overrides(session, 1, updated_by="test", connected_accounts=1)
    with database.session() as session:
        assert quotas_for(session, settings, 1) == UNLIMITED


def test_hosted_quotas_are_defaults_overlaid_with_overrides(
    settings: Settings, database: Database
) -> None:
    hosted = _hosted(settings, quota_posts_per_day=7)
    with database.session() as session:
        defaults = quotas_for(session, hosted, 1)
    assert defaults == WorkspaceQuotas(
        storage_bytes=1024**3, connected_accounts=3, posts_per_day=7, api_requests_per_minute=120
    )
    with database.session() as session:
        set_quota_overrides(session, 1, updated_by="op", connected_accounts=10, posts_per_day=0)
    with database.session() as session:
        quotas = quotas_for(session, hosted, 1)
    assert (quotas.connected_accounts, quotas.posts_per_day, quotas.storage_bytes) == (
        10,
        0,
        1024**3,
    )
    with database.session() as session:
        set_quota_overrides(session, 1, updated_by="op", clear=True)
        with pytest.raises(ValueError, match="unknown quota"):
            set_quota_overrides(session, 1, updated_by="op", seats=3)
    with database.session() as session:
        assert quotas_for(session, hosted, 1) == defaults


def test_publishing_allowed_honours_the_kill_switch_and_the_global_pause(
    settings: Settings, database: Database
) -> None:
    with database.session() as session:
        assert publishing_allowed(session, settings, 1) is True
        set_publishing_disabled(
            session, 1, disabled=True, reason="spam reports", updated_by="operator"
        )
    with database.session() as session:
        gate = publishing_gate(session, settings, 1)
        assert gate.allowed is False
        assert gate.reason is not None and "spam reports" in gate.reason
        set_publishing_disabled(session, 1, disabled=False, reason=None, updated_by="operator")
    with database.session() as session:
        assert publishing_allowed(session, settings, 1) is True
        paused = settings.model_copy(update={"publishing_paused": True})
        assert publishing_allowed(session, paused, 1) is False
        assert publishing_allowed(session, _hosted(paused), 1) is False


def test_operator_cli_toggles_the_kill_switch(
    settings: Settings, database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hosted_commands, "get_settings", lambda: settings)
    runner = CliRunner()
    paused = runner.invoke(cli, ["hosted", "pause-publishing", "1", "--reason", "abuse review"])
    assert paused.exit_code == 0, paused.output
    status = runner.invoke(cli, ["hosted", "status", "1"])
    assert status.exit_code == 0
    # Log lines may precede the report; decode the report object itself.
    report_start = status.output.index('{\n  "workspace_id"')
    report = json.JSONDecoder().raw_decode(status.output[report_start:])[0]
    assert report["publishing_allowed"] is False
    assert report["reason"] == "publishing is disabled for this workspace: abuse review"
    resumed = runner.invoke(cli, ["hosted", "resume-publishing", "1"])
    assert resumed.exit_code == 0
    with database.session() as session:
        assert publishing_allowed(session, settings, 1) is True
    missing = runner.invoke(cli, ["hosted", "status", "999"])
    assert missing.exit_code != 0


def _platform_client(
    app_settings: Settings, database: Database, principal: Principal
) -> TestClient:
    application = FastAPI()
    application.state.settings = app_settings
    application.add_exception_handler(Problem, problem_handler)
    application.include_router(build_platform_router(database, app_settings))
    application.dependency_overrides[get_principal] = lambda: principal
    return TestClient(application, base_url="http://127.0.0.1:8000")


def _member(workspace_id: int = 1) -> Principal:
    return Principal(
        kind="user",
        id="7",
        workspace_id=workspace_id,
        role="owner",
        scopes=scopes_for_role("owner"),
        display="Owner",
    )


def test_connect_is_refused_once_the_connected_accounts_quota_is_reached(
    settings: Settings, database: Database
) -> None:
    hosted = _hosted(settings, platform_provider="mock", quota_connected_accounts=1)
    with database.session() as session:
        session.add(ConnectedAccount(platform="youtube", external_account_id="UC1"))
    client = _platform_client(hosted, database, _member())
    refused = client.post("/api/platforms/instagram/connect")
    assert refused.status_code == 403, refused.text
    assert refused.json()["code"] == "quota_exceeded"

    # A disconnected account frees its slot; the request then reaches the
    # provider step (mock mode has no OAuth, hence 400).
    with database.session() as session:
        account = session.scalar(select(ConnectedAccount))
        assert account is not None
        account.status = "disconnected"
    assert client.post("/api/platforms/instagram/connect").status_code == 400
    # Reconnecting a disconnected account needs a slot again.
    with database.session() as session:
        session.add(ConnectedAccount(platform="tiktok", external_account_id="tt1"))
    assert client.post("/api/platforms/accounts/1/reconnect").json()["code"] == "quota_exceeded"


def test_local_connect_has_no_account_quota(settings: Settings, database: Database) -> None:
    local = settings.model_copy(update={"platform_provider": "mock", "quota_connected_accounts": 0})
    with database.session() as session:
        session.add(ConnectedAccount(platform="youtube", external_account_id="UC1"))
    client = _platform_client(local, database, local_owner())
    # Unchanged local behaviour: mock mode refuses OAuth, no quota is applied.
    assert client.post("/api/platforms/instagram/connect").status_code == 400


def test_require_send_allowed_applies_the_switches_and_the_posts_per_day_quota(
    settings: Settings, database: Database
) -> None:
    hosted = _hosted(settings, quota_posts_per_day=2)
    with database.session() as session:
        require_send_allowed(session, hosted, 1)
        require_send_allowed(session, hosted, 1, posts_sent_today=1)
        with pytest.raises(Problem) as over:
            require_send_allowed(session, hosted, 1, posts_sent_today=2)
        assert over.value.code == "quota_exceeded"
        # Local: unlimited posts, but the pause still applies.
        require_send_allowed(session, settings, 1, posts_sent_today=10_000)
        with pytest.raises(PublishingPaused):
            require_send_allowed(
                session, settings.model_copy(update={"publishing_paused": True}), 1
            )
        set_publishing_disabled(session, 1, disabled=True, reason="abuse", updated_by="op")
    with database.session() as session, pytest.raises(PublishingPaused, match="abuse"):
        require_send_allowed(session, hosted, 1, posts_sent_today=0)


def test_connected_account_slots_count_everything_not_disconnected(
    settings: Settings, database: Database
) -> None:
    hosted = _hosted(settings, quota_connected_accounts=2)
    with database.session() as session:
        session.add(ConnectedAccount(platform="youtube", external_account_id="a"))
        session.add(
            ConnectedAccount(platform="youtube", external_account_id="b", status="needs_reauth")
        )
        session.add(
            ConnectedAccount(platform="youtube", external_account_id="c", status="disconnected")
        )
    with database.session() as session:
        with pytest.raises(Problem, match="403"):
            require_connected_account_slot(session, hosted, 1)
        require_connected_account_slot(session, settings, 1)


def test_connected_account_slots_do_not_leak_across_workspaces(
    settings: Settings, database: Database
) -> None:
    hosted = _hosted(settings, quota_connected_accounts=1)
    with database.session() as session:
        session.add(Workspace(id=2, slug="other-workspace", name="Other workspace"))
        session.flush()
        session.add(
            ConnectedAccount(platform="youtube", external_account_id="theirs", workspace_id=2)
        )
    with database.session() as session:
        require_connected_account_slot(session, hosted, 1)
        with pytest.raises(Problem, match="403"):
            require_connected_account_slot(session, hosted, 2)
