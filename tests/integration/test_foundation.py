from fastapi.testclient import TestClient
from sqlalchemy import text

from runway.api.app import create_app
from runway.config import Settings
from runway.db import Database


def test_migrations_seed_channel_and_enable_wal(database: Database) -> None:
    with database.session() as session:
        channel_count = session.execute(text("SELECT count(*) FROM channels")).scalar_one()
        journal_mode = session.execute(text("PRAGMA journal_mode")).scalar_one()
        foreign_keys = session.execute(text("PRAGMA foreign_keys")).scalar_one()
        migration = session.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        feedback_table = session.execute(
            text(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='caption_feedback'"
            )
        ).scalar_one()
        publisher_table = session.execute(
            text(
                "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='publish_attempts'"
            )
        ).scalar_one()
        schedule_slot = session.execute(
            text(
                "SELECT count(*) FROM pragma_table_info('proposals') "
                "WHERE name='scheduled_publish_at'"
            )
        ).scalar_one()
        planning_horizon = session.execute(
            text("SELECT planning_horizon_days FROM channels LIMIT 1")
        ).scalar_one()
    assert channel_count == 1
    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1
    assert migration == "0006_uncapped_lineup"
    assert feedback_table == 1
    assert publisher_table == 1
    assert schedule_slot == 1
    assert planning_horizon == 0


def test_health_app_is_local_and_publishing_is_disabled(settings: Settings) -> None:
    app = create_app(settings)
    health_route = next(route for route in app.routes if getattr(route, "path", None) == "/health")
    payload = health_route.endpoint()
    assert payload["status"] == "ok"
    assert payload["publishing_enabled"] is False


def test_lineup_mutations_require_literal_confirmation(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        update = client.patch(
            "/api/lineup/1",
            json={"final_caption": "Why now?!", "confirmed": False},
        )
        removal = client.post(
            "/api/lineup/1/remove",
            json={"confirmed": False},
        )

    assert update.status_code == 422
    assert removal.status_code == 422
