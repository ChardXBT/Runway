"""The committed `apps/web/lib/v1/openapi.json` must match the live v1 routes.

The web app generates its typed client from that file. If a route, schema or
response changes without `npm run gen:api`, this test fails with the drift, so
the generated types can never silently disagree with the backend (spec 06
section 6).
"""

from __future__ import annotations

import json
from pathlib import Path

from runway.api.v1.openapi_export import (
    build_export_app,
    render_v1_openapi,
    v1_openapi_document,
)

EXPORT_PATH = Path(__file__).resolve().parents[2] / "apps" / "web" / "lib" / "v1" / "openapi.json"


def test_export_contains_only_v1_paths() -> None:
    document = v1_openapi_document(build_export_app())
    paths = document["paths"]
    assert paths, "the export must contain the /api/v1 routes"
    assert all(path.startswith("/api/v1/") for path in paths)
    # Spot-check the surface the web client types against.
    for expected in (
        "/api/v1/posts",
        "/api/v1/posts/{post_id}",
        "/api/v1/posts/{post_id}/targets/{target_id}/release",
        "/api/v1/media",
        "/api/v1/events/stream",
        "/api/v1/keys",
        "/api/v1/approvals",
        "/api/v1/webhooks",
        "/api/v1/accounts/{account_id}/sync-status",
        "/api/v1/accounts/{account_id}/tiktok/creator-info",
        "/api/v1/accounts/{account_id}/youtube/categories",
        "/api/v1/proposals/{proposal_id}/cross-post",
    ):
        assert expected in paths, f"{expected} is missing from the v1 export"


def test_export_is_serialisable_and_deterministic() -> None:
    document = v1_openapi_document(build_export_app())
    first = render_v1_openapi(document)
    second = render_v1_openapi(v1_openapi_document(build_export_app()))
    json.loads(first)
    assert first == second, "the export must be byte-deterministic"


def test_committed_openapi_json_matches_the_routes() -> None:
    assert EXPORT_PATH.exists(), f"{EXPORT_PATH} is missing; run `npm run gen:api` to create it"
    expected = render_v1_openapi(v1_openapi_document(build_export_app()))
    assert EXPORT_PATH.read_text(encoding="utf-8") == expected, (
        "apps/web/lib/v1/openapi.json is stale; run `npm run gen:api` "
        "and commit the regenerated openapi.json and schema.d.ts"
    )
