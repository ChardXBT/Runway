"""`runway mcp`: the MCP server over stdio, started by an agent host (spec 06 section 7)."""

from __future__ import annotations

import os
import sys

import anyio
import typer

from runway.config import get_settings
from runway.db.base import Database
from runway.db.bootstrap import DatabaseNotCurrent, require_database_at_head
from runway.logging import configure_logging

API_KEY_ENV = "RUNWAY_API_KEY"
EXIT_NOT_MIGRATED = 1
EXIT_REFUSED = 2


def mcp_command() -> None:
    """Serve Runway's MCP tools over stdio for a desktop agent host.

    Set RUNWAY_API_KEY to a Runway API key to act with that key's scopes. With no
    key (local profile only) the server proposes as a keyless local agent and can
    never release a post. See docs/AGENT_API.md.
    """
    # stdout carries the protocol; every log line goes to stderr.
    configure_logging(stream=sys.stderr)
    settings = get_settings()
    api_key = os.environ.get(API_KEY_ENV, "").strip() or None
    if api_key is None and settings.profile != "local":
        typer.echo(f"set {API_KEY_ENV}: the hosted profile has no keyless MCP access", err=True)
        raise typer.Exit(code=EXIT_REFUSED)
    try:
        require_database_at_head(settings)
    except DatabaseNotCurrent as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=EXIT_NOT_MIGRATED) from exc
    from runway.mcp.server import build_mcp_server, build_runner, serve_stdio

    database = Database(settings)
    try:
        server = build_mcp_server(build_runner(database, settings, api_key))
        anyio.run(serve_stdio, server)
    finally:
        database.engine.dispose()
