"""Deterministic `/api/v1` OpenAPI export for the web client's typed surface.

The web app generates its `schema.d.ts` from a committed `openapi.json`. This
module is the single source for that file's bytes: the export script writes it
and a contract test regenerates it and compares, so a route change that forgets
`npm run gen:api` fails CI (spec 06 section 6).

Determinism: the document is rendered with sorted keys and fixed indentation,
and the app is built with fixed settings in a disposable data directory, so the
bytes depend only on the route definitions, never on the operator's `.env`.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import FastAPI

from runway.api.app import create_app
from runway.config import Settings

V1_PREFIX = "/api/v1"

# Where `npm run gen:api` commits the document, relative to the repository root.
EXPORT_PATH = Path("apps") / "web" / "lib" / "v1" / "openapi.json"


def build_export_app() -> FastAPI:
    """Build the local-profile app offline, with a disposable data directory.

    `_env_file=None` keeps `.env` out of the export: the document must depend on
    the checked-in code only, never on secrets or the operator's settings.
    """
    settings = Settings(
        data_dir=Path(tempfile.mkdtemp(prefix="runway-openapi-export-")),
        platform_provider="mock",
        publishing_enabled=False,
        agent_runtime="mock",
        _env_file=None,
    )
    settings.ensure_directories()
    return create_app(settings)


def v1_openapi_document(app: FastAPI) -> dict[str, object]:
    """`app.openapi()` reduced to the `/api/v1` paths.

    The unversioned routes stay out of the committed document: the web client's
    typed surface is the versioned contract only.
    """
    document = app.openapi()
    paths = document.get("paths")
    if not isinstance(paths, dict):
        raise RuntimeError("the application produced an OpenAPI document without paths")
    document["paths"] = {path: paths[path] for path in sorted(paths) if path.startswith(V1_PREFIX)}
    return document


def render_v1_openapi(document: dict[str, object]) -> str:
    """Serialise deterministically: sorted keys, two-space indent, LF newline."""
    return json.dumps(document, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def export_v1_openapi() -> str:
    """The exact bytes `apps/web/lib/v1/openapi.json` should contain."""
    return render_v1_openapi(v1_openapi_document(build_export_app()))
