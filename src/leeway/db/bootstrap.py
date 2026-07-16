from __future__ import annotations

from alembic.config import Config
from sqlalchemy import select

from alembic import command
from leeway.config import Settings
from leeway.db.base import Database
from leeway.db.models import Channel


def run_migrations(settings: Settings) -> None:
    config = Config(str(settings.project_root / "alembic.ini"))
    config.set_main_option("script_location", str(settings.project_root / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")


def initialize_database(settings: Settings) -> Database:
    settings.ensure_directories()
    run_migrations(settings)
    database = Database(settings)
    with database.session() as session:
        channel = session.scalar(select(Channel).where(Channel.handle == settings.channel_handle))
        if channel is None:
            session.add(
                Channel(
                    name=settings.channel_name,
                    handle=settings.channel_handle,
                    timezone=settings.timezone,
                    default_post_time=settings.default_post_time,
                    planning_horizon_days=settings.planning_horizon_days,
                    duplicate_window_days=settings.duplicate_window_days,
                )
            )
    return database
