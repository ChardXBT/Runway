from __future__ import annotations

from dataclasses import dataclass

from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, select

from alembic import command
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import Channel


class DatabaseNotCurrent(RuntimeError):
    """The configured database is not at the migration head this code expects."""


@dataclass(frozen=True, slots=True)
class MigrationStatus:
    current: str | None
    head: str
    known: bool

    @property
    def up_to_date(self) -> bool:
        return self.current == self.head

    def as_dict(self) -> dict[str, object]:
        return {
            "current": self.current,
            "head": self.head,
            "up_to_date": self.up_to_date,
            "known_revision": self.known,
        }


def alembic_config(settings: Settings) -> Config:
    config = Config(str(settings.project_root / "alembic.ini"))
    config.set_main_option("script_location", str(settings.project_root / "alembic"))
    config.set_main_option("sqlalchemy.url", settings.database_url)
    # Read by 0013_workspaces to name workspace 1 after the configured channel.
    config.attributes["runway_channel_name"] = settings.channel_name
    return config


def run_migrations(settings: Settings) -> None:
    command.upgrade(alembic_config(settings), "head")


def migration_status(settings: Settings) -> MigrationStatus:
    """Read the database revision without creating or altering anything."""
    script = ScriptDirectory.from_config(alembic_config(settings))
    head = script.get_current_head()
    if head is None:
        raise RuntimeError("the migration script directory has no head revision")
    current: str | None = None
    path = settings.database_path
    if path.is_file():
        engine = create_engine(
            f"sqlite+pysqlite:///file:{path.resolve().as_posix()}?mode=ro&uri=true",
            future=True,
        )
        try:
            with engine.connect() as connection:
                current = MigrationContext.configure(connection).get_current_revision()
        finally:
            engine.dispose()
    known = current is None or current in {
        revision.revision for revision in script.walk_revisions()
    }
    return MigrationStatus(current=current, head=head, known=known)


def require_database_at_head(settings: Settings) -> MigrationStatus:
    """Refuse to serve a database that is behind, ahead of, or unknown to this code."""
    status = migration_status(settings)
    if status.up_to_date:
        return status
    if status.current is None:
        problem = f"the database at {settings.database_path} has not been migrated"
    elif not status.known:
        problem = (
            f"the database is at revision {status.current}, which this code does not know "
            "(was it upgraded by a newer checkout?)"
        )
    else:
        problem = f"the database is at revision {status.current}, behind head {status.head}"
    raise DatabaseNotCurrent(
        f"Runway will not start: {problem}. Run `runway db upgrade` first "
        "(the launcher does this before starting the API)."
    )


def ensure_default_channel(database: Database, settings: Settings) -> None:
    with database.session() as session:
        channel = session.scalar(select(Channel).where(Channel.handle == settings.channel_handle))
        if channel is None:
            session.add(
                Channel(
                    name=settings.channel_name,
                    handle=settings.channel_handle,
                    timezone=settings.timezone,
                    default_post_time=settings.default_post_time,
                    # Legacy non-null column retained for database compatibility.
                    # Zero means the current scheduler has no horizon cap.
                    planning_horizon_days=0,
                    duplicate_window_days=settings.duplicate_window_days,
                )
            )


def initialize_database(settings: Settings) -> Database:
    """Upgrade the configured database to head and seed the configured channel.

    This is the explicit migration entry point (`runway db upgrade`, CLI
    commands, and test fixtures). The API never calls it.
    """
    settings.ensure_directories()
    run_migrations(settings)
    database = Database(settings)
    ensure_default_channel(database, settings)
    return database
