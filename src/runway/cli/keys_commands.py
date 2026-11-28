"""`runway keys`: create, list and revoke API keys as the local owner (spec 06 section 3).

The secret is printed once, by `create`, and never again. These commands act
as the built-in owner of workspace 1, so they work in the `local` profile only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import typer

from runway.api.v1.pagination import CursorParams
from runway.auth.api_keys import DEFAULT_AGENT_SCOPES, ApiKeyService
from runway.auth.principal import ALL_SCOPES, local_owner
from runway.auth.problems import Problem
from runway.config import Settings, get_settings
from runway.db.base import Database
from runway.db.bootstrap import DatabaseNotCurrent, require_database_at_head

keys_app = typer.Typer(help="Create, list and revoke API keys for agents and scripts.")

EXIT_REFUSED = 2


def _service() -> tuple[ApiKeyService, Settings]:
    settings = get_settings()
    if settings.profile != "local":
        typer.echo("runway keys works in the local profile only; use /api/v1/keys", err=True)
        raise typer.Exit(code=EXIT_REFUSED)
    try:
        require_database_at_head(settings)
    except DatabaseNotCurrent as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    return ApiKeyService(Database(settings), settings), settings


def _fail(problem: Problem) -> typer.Exit:
    typer.echo(f"{problem.code}: {problem.detail}", err=True)
    return typer.Exit(code=EXIT_REFUSED)


@keys_app.command("create")
def keys_create(
    name: str = typer.Option(..., "--name", help="What the key is for, e.g. 'Muse'."),
    scope: list[str] = typer.Option(
        [],
        "--scope",
        help=(
            "A scope to grant; repeat for several. Without any, the default agent scopes: "
            + ", ".join(sorted(DEFAULT_AGENT_SCOPES))
        ),
    ),
    expires_days: int | None = typer.Option(
        None, "--expires-days", min=1, max=3650, help="Expire the key after this many days."
    ),
) -> None:
    """Create a key and print its secret once. Runway keeps only a hash."""
    service, _settings = _service()
    unknown = sorted(set(scope) - ALL_SCOPES)
    if unknown:
        typer.echo(f"unknown scopes: {', '.join(unknown)}", err=True)
        raise typer.Exit(code=EXIT_REFUSED)
    expires_at = datetime.now(UTC) + timedelta(days=expires_days) if expires_days else None
    try:
        created = service.create(
            local_owner(), name=name, scopes=scope or None, expires_at=expires_at
        )
    except Problem as problem:
        raise _fail(problem) from problem
    typer.echo(json.dumps(created.key.model_dump(mode="json"), indent=2))
    typer.echo("")
    typer.echo("Secret (shown once; store it now, Runway cannot show it again):")
    typer.echo(created.secret)


@keys_app.command("list")
def keys_list() -> None:
    """List the workspace's keys. Secrets are never shown."""
    service, _settings = _service()
    items: list[dict[str, object]] = []
    cursor: str | None = None
    while True:
        page = service.list(local_owner().workspace_id, CursorParams(cursor=cursor, limit=200))
        items.extend(item.model_dump(mode="json") for item in page.items)
        if page.next_cursor is None:
            break
        cursor = page.next_cursor
    typer.echo(json.dumps(items, indent=2))


@keys_app.command("revoke")
def keys_revoke(key_id: int = typer.Argument(..., min=1, help="The key id from `list`.")) -> None:
    """Revoke a key at once."""
    service, _settings = _service()
    try:
        view = service.revoke(local_owner(), key_id)
    except Problem as problem:
        raise _fail(problem) from problem
    typer.echo(json.dumps(view.model_dump(mode="json"), indent=2))
