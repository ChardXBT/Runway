from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from runway.cli.main import app
from runway.config import get_settings
from runway.db import initialize_database
from runway.db.models import RepresentationSet
from runway.db.repositories import get_channel


def _json_output(value: str) -> dict[str, object]:
    return json.loads(value[value.index("{") :])


def _configure_data_dir(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("RUNWAY_DATA_DIR", str(path))
    monkeypatch.setenv("RUNWAY_PUBLISHING_ENABLED", "false")
    monkeypatch.setenv("RUNWAY_AGENT_RUNTIME", "mock")
    get_settings.cache_clear()


def test_database_doctor_json_and_schema_verify_exit_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_data_dir(monkeypatch, tmp_path / "doctor")
    runner = CliRunner()
    schema = runner.invoke(app, ["database", "schema-verify"])
    assert schema.exit_code == 0, schema.output
    assert _json_output(schema.output)["matches"] is True

    doctor = runner.invoke(
        app,
        [
            "database",
            "intelligence-doctor",
            "--json",
            "--skip-media-files",
        ],
    )
    assert doctor.exit_code == 0, doctor.output
    payload = _json_output(doctor.output)
    assert payload["status"] == "passed"
    assert payload["counts"]["critical"] == 0


def test_provider_status_is_no_download_and_representation_plan_is_inactive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_data_dir(monkeypatch, tmp_path / "providers")
    runner = CliRunner()
    providers = runner.invoke(app, ["intelligence", "provider-status"])
    assert providers.exit_code == 0, providers.output
    status = json.loads(providers.output)
    assert status["automatic_downloads"] is False
    assert status["implicit_activation"] is False

    planned = runner.invoke(
        app,
        [
            "intelligence",
            "representations",
            "plan",
            "--modality",
            "text",
        ],
    )
    assert planned.exit_code == 0, planned.output
    result = _json_output(planned.output)
    assert result["active"] is False
    assert result["expected"] == 0


def test_intelligence_doctor_returns_nonzero_for_critical_findings(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_data_dir(monkeypatch, tmp_path / "critical-doctor")
    settings = get_settings()
    database = initialize_database(settings)
    with database.session() as session:
        channel_id = get_channel(session, settings.channel_handle).id
        for index in range(2):
            session.add(
                RepresentationSet(
                    channel_id=channel_id,
                    scope="historical_text",
                    purpose="historical_caption_semantics",
                    modality="text",
                    provider="runway-local",
                    model="fixture",
                    model_version="1",
                    configuration_json="{}",
                    configuration_hash=f"{index + 1:064x}",
                    plan_hash=f"{index + 10:064x}",
                    status="active",
                    expected_count=0,
                    completed_count=0,
                    failed_count=0,
                    stale_count=0,
                    active=True,
                )
            )

    result = CliRunner().invoke(
        app,
        [
            "database",
            "intelligence-doctor",
            "--json",
            "--skip-media-files",
        ],
    )
    assert result.exit_code == 1
    payload = _json_output(result.output)
    assert payload["status"] == "failed"
    assert payload["counts"]["critical"] >= 1
