"""Operator inspection commands for durable worker jobs."""

from __future__ import annotations

import json
import sys

import typer

from runway.config import get_settings
from runway.db.base import Database
from runway.db.bootstrap import DatabaseNotCurrent, require_database_at_head
from runway.jobs.queue import JobQueue
from runway.jobs.runtime import build_worker
from runway.logging import configure_logging

jobs_app = typer.Typer(help="Inspect durable worker jobs.", no_args_is_help=True)


@jobs_app.command("unknown")
def unknown_jobs() -> None:
    """List pending job kinds the currently configured worker cannot run."""
    # stdout is the machine-readable operator surface; migration/library logs are diagnostics.
    configure_logging(stream=sys.stderr)
    settings = get_settings()
    try:
        require_database_at_head(settings)
    except DatabaseNotCurrent as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    database = Database(settings)
    try:
        # Building the real worker keeps this inspection in lock-step with the
        # handler registry instead of duplicating a list of job-kind strings.
        worker = build_worker(settings, database)
        counts = JobQueue(database).unknown_pending_counts(worker.handlers.keys())
    finally:
        database.engine.dispose()
    payload = {
        "policy": "pending_for_rolling_deploy_compatibility",
        "unknown_pending_jobs": [
            {"kind": kind, "pending": count} for kind, count in counts.items()
        ],
        "operator_action": (
            "deploy a worker that handles the kind; if the kind was produced in error, "
            "inspect the publish_jobs row before changing it"
        ),
    }
    typer.echo(json.dumps(payload, indent=2))
