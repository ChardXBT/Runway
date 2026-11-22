"""Per-workspace quotas, the workspace kill switch and the global publishing pause."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from runway.cli import hosted_commands
from runway.cli.main import app as cli
from runway.config import Settings
from runway.db import Database
from runway.hosted.quotas import (
    UNLIMITED,
    WorkspaceQuotas,
    publishing_allowed,
    publishing_gate,
    quotas_for,
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
