"""`runway hosted`: operator controls for hosted workspaces (spec 05 section 6).

The global switch is the `RUNWAY_PUBLISHING_PAUSED` setting; these commands
manage the per-workspace kill switch and quota overrides in the database.
"""

from __future__ import annotations

import json
from dataclasses import asdict

import typer

from runway.config import get_settings
from runway.db.base import Database
from runway.db.bootstrap import require_database_at_head
from runway.db.models import Workspace
from runway.hosted.quotas import (
    publishing_gate,
    quotas_for,
    set_publishing_disabled,
    set_quota_overrides,
)

hosted_app = typer.Typer(help="Operator controls for hosted workspaces: kill switch and quotas.")

OPERATOR = "operator-cli"


def _database() -> Database:
    settings = get_settings()
    require_database_at_head(settings)
    return Database(settings)


def _require_workspace(database: Database, workspace_id: int) -> None:
    with database.session() as session:
        if session.get(Workspace, workspace_id) is None:
            raise typer.BadParameter(f"workspace {workspace_id} does not exist")


@hosted_app.command("status")
def hosted_status(workspace_id: int = typer.Argument(..., min=1)) -> None:
    """Show a workspace's effective quotas and whether it may publish."""
    settings = get_settings()
    database = _database()
    _require_workspace(database, workspace_id)
    with database.session() as session:
        gate = publishing_gate(session, settings, workspace_id)
        quotas = quotas_for(session, settings, workspace_id)
    typer.echo(
        json.dumps(
            {
                "workspace_id": workspace_id,
                "profile": settings.profile,
                "publishing_paused_globally": settings.publishing_paused,
                "publishing_allowed": gate.allowed,
                "reason": gate.reason,
                "quotas": asdict(quotas),
            },
            indent=2,
        )
    )


@hosted_app.command("pause-publishing")
def pause_publishing(
    workspace_id: int = typer.Argument(..., min=1),
    reason: str = typer.Option(..., "--reason", help="Recorded and shown to the workspace."),
) -> None:
    """Kill switch: stop every send for one workspace until resumed."""
    database = _database()
    _require_workspace(database, workspace_id)
    with database.session() as session:
        set_publishing_disabled(
            session, workspace_id, disabled=True, reason=reason, updated_by=OPERATOR
        )
    typer.echo(f"publishing disabled for workspace {workspace_id}")


@hosted_app.command("resume-publishing")
def resume_publishing(workspace_id: int = typer.Argument(..., min=1)) -> None:
    """Lift the per-workspace kill switch (the global pause is a setting)."""
    database = _database()
    _require_workspace(database, workspace_id)
    with database.session() as session:
        set_publishing_disabled(
            session, workspace_id, disabled=False, reason=None, updated_by=OPERATOR
        )
    typer.echo(f"publishing enabled for workspace {workspace_id}")


@hosted_app.command("quota")
def set_quota(
    workspace_id: int = typer.Argument(..., min=1),
    storage_bytes: int | None = typer.Option(None, min=0),
    connected_accounts: int | None = typer.Option(None, min=0),
    posts_per_day: int | None = typer.Option(None, min=0),
    api_requests_per_minute: int | None = typer.Option(None, min=1),
    clear: bool = typer.Option(False, help="Reset every override to the configured default."),
) -> None:
    """Override one workspace's quotas; options left out keep their current value."""
    database = _database()
    _require_workspace(database, workspace_id)
    with database.session() as session:
        set_quota_overrides(
            session,
            workspace_id,
            updated_by=OPERATOR,
            clear=clear,
            storage_bytes=storage_bytes,
            connected_accounts=connected_accounts,
            posts_per_day=posts_per_day,
            api_requests_per_minute=api_requests_per_minute,
        )
    hosted_status(workspace_id)
