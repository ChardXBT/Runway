"""`runway db`: migrate and inspect the configured database.

Every migration path (this module, `runway init`, CLI commands that call
`initialize_database`) holds the same per-data-directory migration lock.

Also runnable as `python -m runway.cli.db_commands` (the launcher uses this:
it starts in about a third of the time of the full CLI).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

import typer
from alembic.script import ScriptDirectory

from alembic import command
from runway.config import Settings, get_settings
from runway.db.backup import AUTOMATIC_BACKUPS_KEPT, backup_database, prune_automatic_backups
from runway.db.bootstrap import (
    alembic_config,
    initialize_database,
    migration_lock,
    migration_lock_path,
    migration_status,
)
from runway.locks import LockTimeout, exclusive_file_lock
from runway.operations.runtime_files import worker_lock_path

db_app = typer.Typer(help="Migrate and inspect the configured database.")

# The oldest schema the expansion code can roll back to without --force:
# the last revision the pre-expansion release (main) understands.
OLDEST_SUPPORTED_DOWNGRADE = "0012_creator_platforms"
UPGRADE_BACKUP_REASON = "before-upgrade"


@db_app.command("upgrade")
def db_upgrade(
    backup: bool = typer.Option(
        True,
        "--backup/--no-backup",
        help="Take an online SQLite backup first when migrations are pending (default on).",
    ),
) -> None:
    """Upgrade the configured database to the newest migration and seed defaults."""
    settings = get_settings()
    with _only_migration(settings):
        _upgrade(settings, backup)


def _upgrade(settings: Settings, backup: bool) -> None:
    before = migration_status(settings)
    if not before.up_to_date:
        # Rebuilding tables under a running worker (older code that accepted
        # the old schema) is unsafe; the launcher stops it first.
        _refuse_while_the_worker_runs(settings, "upgrading")
    backup_path = None
    if backup and before.current is not None and not before.up_to_date and not settings.is_postgres:
        # SQLite online backup only; Postgres backups are pg_dump's job.
        backup_path = backup_database(settings, UPGRADE_BACKUP_REASON)
        prune_automatic_backups(settings, UPGRADE_BACKUP_REASON, AUTOMATIC_BACKUPS_KEPT)
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
                "backup": str(backup_path) if backup_path else None,
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


EXIT_WORKER_RUNNING = 3
EXIT_MIGRATION_RUNNING = 4

__all__ = ["EXIT_MIGRATION_RUNNING", "EXIT_WORKER_RUNNING", "db_app", "migration_lock_path"]


@contextmanager
def _only_migration(settings: Settings) -> Iterator[None]:
    """One `runway db upgrade/downgrade` at a time per data directory.

    The launcher stops waiting for a slow migration but leaves it running; the
    next launcher run must not start a second one beside it.
    """
    with ExitStack() as stack:
        # Acquired outside the body's try, so a LockTimeout raised by the
        # migration itself is never reported as "another migration is running".
        try:
            stack.enter_context(migration_lock(settings, timeout=0))
        except LockTimeout:
            typer.echo("another `runway db` migration is running; wait for it to finish", err=True)
            raise typer.Exit(code=EXIT_MIGRATION_RUNNING) from None
        yield


def _refuse_while_the_worker_runs(settings: Settings, action: str) -> None:
    try:
        with exclusive_file_lock(worker_lock_path(settings), timeout=0):
            pass
    except LockTimeout:
        typer.echo(
            f"stop `runway worker` before {action} the database "
            "(it holds data/runtime/worker.lock; its PID is in data/runtime/worker.pid)",
            err=True,
        )
        raise typer.Exit(code=EXIT_WORKER_RUNNING) from None


def _supported_targets(script: ScriptDirectory) -> set[str]:
    return {
        revision.revision
        for revision in script.walk_revisions(base=OLDEST_SUPPORTED_DOWNGRADE, head="heads")
    }


@db_app.command("downgrade")
def db_downgrade(
    revision: str = typer.Argument(..., help="Target revision, e.g. 0012_creator_platforms."),
    force: bool = typer.Option(
        False, "--force", help=f"Allow targets older than {OLDEST_SUPPORTED_DOWNGRADE}."
    ),
    i_have_a_backup: bool = typer.Option(
        False,
        "--i-have-a-backup",
        help="Postgres only: confirm an external pg_dump exists (SQLite backs up online instead).",
    ),
) -> None:
    """Roll the schema back to REVISION, after an online backup.

    Run it with the NEW checkout (it holds the downgrade steps), with the API
    and worker stopped, then check out the older code.
    """
    settings = get_settings()
    if settings.is_postgres and not i_have_a_backup:
        typer.echo(
            "refusing to downgrade a Postgres database without a backup. "
            "Take one first, for example:\n"
            "  pg_dump --format=custom --file=runway-before-downgrade.dump <database>\n"
            "then re-run with --i-have-a-backup.",
            err=True,
        )
        raise typer.Exit(code=2)
    status = migration_status(settings)
    if status.current is None:
        typer.echo("there is no migrated database to downgrade", err=True)
        raise typer.Exit(code=1)
    config = alembic_config(settings)
    script = ScriptDirectory.from_config(config)
    if not force and revision not in _supported_targets(script):
        typer.echo(
            f"refusing to downgrade to {revision!r}: the supported targets are "
            f"{OLDEST_SUPPORTED_DOWNGRADE} and newer (use --force to override)",
            err=True,
        )
        raise typer.Exit(code=2)
    with _only_migration(settings):
        _refuse_while_the_worker_runs(settings, "downgrading")
        backup_path = (
            None
            if settings.is_postgres
            else backup_database(settings, f"before-downgrade-to-{revision}")
        )
        command.downgrade(config, revision)
        after = migration_status(settings)
    typer.echo(
        json.dumps(
            {
                "database": str(settings.database_path),
                "previous": status.current,
                "current": after.current,
                "backup": str(backup_path),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    db_app()
