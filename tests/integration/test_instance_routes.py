"""A standby machine cannot mint, store, revoke or use tokens for platform writes via the API."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from runway.api.app import create_app
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import CommentRecord, ConnectedAccount, PlatformAction
from runway.operations import instance_lease
from runway.operations.instance_lease import Lease, read_lease, write_lease
from runway.platforms.tokens import TokenStore

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def remote_is_local(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The private repository's lease equals the local one; no git or gh runs."""
    monkeypatch.setenv("RUNWAY_MACHINE_ID_FILE", str(tmp_path / "profile" / "machine-id"))

    def read_local(settings: Settings) -> Lease | None:
        assert settings.resolved_instance_dir is not None
        return read_lease(settings.resolved_instance_dir)

    monkeypatch.setattr(instance_lease, "remote_lease_reader", read_local)


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
    # The lease changed "remotely": drop the cached confirmation.
    (settings.resolved_instance_dir / instance_lease.LEASE_CHECK_FILE_NAME).unlink(missing_ok=True)


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
        youtube_client_secret="yt-fixture",
    )
    _hold(settings, "other-machine")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        response = client.post("/api/platforms/youtube/connect")
        assert response.status_code == 409
        assert "standby" in response.json()["detail"]


def test_standby_inbox_reply_is_refused_before_anything_is_sent(tmp_path: Path) -> None:
    settings = _instance_settings(tmp_path, platform_provider="mock", inbox_replies_enabled=True)
    _hold(settings, "this-machine")
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/platforms/accounts").json(), "mock accounts are seeded"
        database: Database = app.state.database
        with database.session() as session:
            comment_ids = list(
                session.scalars(
                    select(CommentRecord.id).where(CommentRecord.platform == "instagram")
                ).all()
            )
        assert len(comment_ids) >= 2, "mock seeding creates comments"

        _hold(settings, "other-machine")
        refused = client.post(
            f"/api/inbox/{comment_ids[0]}/reply", json={"text": "Thanks!", "confirmed": True}
        )
        assert refused.status_code == 409
        assert "standby" in refused.json()["detail"]
        with database.session() as session:
            assert session.scalar(select(func.count(PlatformAction.id))) == 0

        # The same request succeeds on the machine that holds the lease.
        _hold(settings, "this-machine")
        sent = client.post(
            f"/api/inbox/{comment_ids[1]}/reply", json={"text": "Thanks!", "confirmed": True}
        )
        assert sent.status_code == 200, sent.text
        assert not instance_lease.worker_is_busy(settings)


def test_lease_heartbeat_runs_only_for_a_private_instance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from runway.operations import instance as ops

    beats: list[str] = []
    monkeypatch.setattr(ops.LeaseHeartbeat, "beat", lambda self: beats.append("beat"))

    def heartbeat_threads() -> list[threading.Thread]:
        return [t for t in threading.enumerate() if t.name == "runway-lease-heartbeat"]

    local = Settings(data_dir=tmp_path / "local-data", platform_provider="mock")
    with TestClient(create_app(local), base_url="http://127.0.0.1:8000"):
        assert heartbeat_threads() == []
    assert beats == []

    settings = _instance_settings(tmp_path, platform_provider="mock")
    _hold(settings, "this-machine")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000"):
        assert len(heartbeat_threads()) == 1
    assert beats, "the heartbeat beats once at start"
    assert heartbeat_threads() == []
