from sqlalchemy import text

from leeway.api.app import create_app
from leeway.config import Settings
from leeway.db import Database


def test_migrations_seed_channel_and_enable_wal(database: Database) -> None:
    with database.session() as session:
        channel_count = session.execute(text("SELECT count(*) FROM channels")).scalar_one()
        journal_mode = session.execute(text("PRAGMA journal_mode")).scalar_one()
        foreign_keys = session.execute(text("PRAGMA foreign_keys")).scalar_one()
    assert channel_count == 1
    assert journal_mode.lower() == "wal"
    assert foreign_keys == 1


def test_health_app_is_local_and_publishing_is_disabled(settings: Settings) -> None:
    app = create_app(settings)
    health_route = next(route for route in app.routes if getattr(route, "path", None) == "/health")
    payload = health_route.endpoint()
    assert payload["status"] == "ok"
    assert payload["publishing_enabled"] is False
