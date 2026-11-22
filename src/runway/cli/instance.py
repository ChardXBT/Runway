from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer

from runway.config import get_settings
from runway.operations import instance as operations
from runway.operations.instance import INSTANCE_ERRORS, InstanceContext

instance_app = typer.Typer(
    help=(
        "Keep this instance's settings, encrypted secrets, tokens and snapshots in a "
        "private companion repository."
    )
)

WAIT_OPTION = typer.Option(
    operations.DEFAULT_WORKER_WAIT_SECONDS,
    "--wait-seconds",
    min=0,
    help="How long to wait for a running publish to finish.",
)


def _run(action: Callable[[InstanceContext], dict[str, Any]]) -> None:
    try:
        result = action(InstanceContext(settings=get_settings()))
    except INSTANCE_ERRORS as exc:
        typer.echo(f"Refused: {exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, indent=2, default=str))


@instance_app.command("init")
def init_command(
    repo: str = typer.Option(..., "--repo", help="owner/name of the PRIVATE instance repository."),
    directory: Path | None = typer.Option(
        None, "--dir", help="Where to clone it; defaults to RUNWAY_INSTANCE_DIR."
    ),
) -> None:
    """Verify the repository is private, clone it and create the instance layout."""
    _run(lambda ctx: operations.init(ctx, repo, directory=directory))


@instance_app.command("status")
def status_command(
    refresh: bool = typer.Option(
        False, "--refresh", help="Fast-forward the instance clone from its remote first."
    ),
) -> None:
    """Show the lease holder, the last snapshot and drift from it."""
    _run(lambda ctx: operations.status(ctx, refresh=refresh))


@instance_app.command("push")
def push_command(wait_seconds: int = WAIT_OPTION) -> None:
    """Snapshot the database and media to the private release and commit the small files."""
    _run(lambda ctx: operations.push(ctx, wait_seconds=wait_seconds))


@instance_app.command("pull")
def pull_command(
    take: bool = typer.Option(
        False, "--take", help="Take a lease whose holder has been silent for over 24 hours."
    ),
    force: bool = typer.Option(
        False,
        "--force",
        help="Replace a local database that changed after the snapshot (it is backed up first).",
    ),
    wait_seconds: int = WAIT_OPTION,
) -> None:
    """Verify the latest snapshot, take the lease and restore it on this machine."""
    _run(lambda ctx: operations.pull(ctx, take=take, force=force, wait_seconds=wait_seconds))


@instance_app.command("handover")
def handover_command(wait_seconds: int = WAIT_OPTION) -> None:
    """Push, then release the lease so another machine can pull."""
    _run(lambda ctx: operations.handover(ctx, wait_seconds=wait_seconds))


@instance_app.command("heartbeat")
def heartbeat_command() -> None:
    """Renew this machine's lease heartbeat if it is due (Runway also does this hourly)."""
    _run(operations.heartbeat)
