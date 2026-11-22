"""`runway db`: the only way the configured database is migrated."""

from __future__ import annotations

import json

import typer

from runway.config import get_settings
from runway.db.bootstrap import initialize_database, migration_status

db_app = typer.Typer(help="Migrate and inspect the configured database.")


@db_app.command("upgrade")
def db_upgrade() -> None:
    """Upgrade the configured database to the newest migration and seed defaults."""
    settings = get_settings()
    before = migration_status(settings)
    database = initialize_database(settings)
    database.engine.dispose()
    after = migration_status(settings)
    typer.echo(
        json.dumps(
            {
                "database": str(settings.database_path),
                "previous": before.current,
                "current": after.current,
                "head": after.head,
                "up_to_date": after.up_to_date,
            },
            indent=2,
        )
    )
    if not after.up_to_date:
        raise typer.Exit(code=1)


@db_app.command("current")
def db_current() -> None:
    """Show the database revision and the head this code expects, read-only."""
    settings = get_settings()
    status = migration_status(settings)
    typer.echo(json.dumps({"database": str(settings.database_path), **status.as_dict()}, indent=2))
