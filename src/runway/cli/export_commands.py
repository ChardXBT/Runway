"""`runway export`: write the local workspace's data export (spec 05 section 5).

The local owner needs no route round-trip: the command builds the same
document the `export_workspace` job produces and writes it to a file.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer
from sqlalchemy import select

from runway.config import get_settings
from runway.db.base import Database
from runway.db.bootstrap import DatabaseNotCurrent, require_database_at_head
from runway.db.models import Workspace
from runway.workspaces.lifecycle import export_workspace_data

EXIT_REFUSED = 2


def export_command(
    output: Path = typer.Option(
        None,
        "--output",
        "-o",
        help="Where to write the JSON document (default: <data dir>/exports/workspace-<id>.json).",
    ),
) -> None:
    """Write every row the local workspace owns to one JSON document."""
    settings = get_settings()
    try:
        require_database_at_head(settings)
    except DatabaseNotCurrent as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=EXIT_REFUSED) from None
    database = Database(settings)
    # A maintenance command, not a request: the export filters the workspace
    # it names, so it runs context-free like the other CLI sweeps.
    with database.system_session() as session:
        workspace_id = session.scalar(select(Workspace.id).limit(1))
        if workspace_id is None:
            typer.echo("no workspace exists yet", err=True)
            raise typer.Exit(code=EXIT_REFUSED)
        document = export_workspace_data(session, int(workspace_id))
    path = output or (settings.resolved_data_dir / "exports" / f"workspace-{workspace_id}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True), encoding="utf-8")
    typer.echo(f"wrote {path}")
