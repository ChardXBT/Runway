"""A standby machine cannot mint, store or revoke platform tokens through the API."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from runway.api.app import create_app
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ConnectedAccount
from runway.operations.instance_lease import Lease, write_lease
from runway.platforms.tokens import TokenStore

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


def _instance_settings(tmp_path: Path, **overrides: object) -> Settings:
    instance_dir = tmp_path / "runway-instance"
    instance_dir.mkdir(exist_ok=True)
    return Settings(
        data_dir=tmp_path / "data",
        instance_dir=instance_dir,
        machine_id="this-machine",
        token_key=Fernet.generate_key().decode("ascii"),
        **overrides,  # type: ignore[arg-type]
    )


def _hold(settings: Settings, holder: str) -> None:
    assert settings.resolved_instance_dir is not None
    write_lease(
        settings.resolved_instance_dir,
        Lease(holder=holder, holder_since=NOW, heartbeat_at=NOW),
    )


def test_standby_disconnect_is_refused_before_revoking(tmp_path: Path) -> None:
    settings = _instance_settings(tmp_path, platform_provider="mock")
    _hold(settings, "this-machine")  # active while the mock accounts are seeded
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        account = client.get("/api/platforms/accounts").json()[0]
        database: Database = app.state.database
        with database.session() as session:
            record = session.get(ConnectedAccount, account["id"])
            assert record is not None and record.token_ref
            token_ref = record.token_ref

        _hold(settings, "other-machine")
        response = client.post(
            f"/api/platforms/accounts/{account['id']}/disconnect",
            json={"confirmed": True},
        )

        assert response.status_code == 409
        assert "standby" in response.json()["detail"]
        assert TokenStore.from_settings(settings).load(token_ref) is not None
        with database.session() as session:
            record = session.get(ConnectedAccount, account["id"])
            assert record is not None and record.status == "connected"


def test_standby_connect_is_refused(tmp_path: Path) -> None:
    settings = _instance_settings(
        tmp_path,
        platform_provider="live",
        youtube_client_id="yt-client",
        youtube_client_secret="yt-secret",
    )
    _hold(settings, "other-machine")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        response = client.post("/api/platforms/youtube/connect")
        assert response.status_code == 409
        assert "standby" in response.json()["detail"]
