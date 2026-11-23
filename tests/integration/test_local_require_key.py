"""RUNWAY_LOCAL_REQUIRE_KEY_FOR_WRITES and its legacy exemption list (spec 06 sections 3 and 9)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from starlette.requests import Request

from runway.api.app import create_app
from runway.auth.principal import local_owner
from runway.auth.problems import Problem
from runway.auth.request_policy import (
    LEGACY_WRITE_EXEMPTIONS,
    enforce_request_policy,
    is_v1_path,
)
from runway.config import Settings
from runway.db.base import Database

BASE_URL = "http://127.0.0.1:8000"
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def _write_routes(application: FastAPI) -> dict[tuple[str, str], APIRoute]:
    routes: dict[tuple[str, str], APIRoute] = {}
    for context in iter_route_contexts(application.routes):
        route = context.original_route
        if not isinstance(route, APIRoute):
            continue
        for method in (context.methods or set()) & MUTATING:
            routes[(method, str(context.path))] = route
    return routes


@pytest.fixture()
def strict_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"local_require_key_for_writes": True})


@pytest.fixture()
def strict_client(strict_settings: Settings, database: Database) -> Iterator[TestClient]:
    with TestClient(create_app(strict_settings), base_url=BASE_URL) as client:
        yield client


def test_the_exemption_list_is_exactly_the_unversioned_write_routes(tmp_path: Path) -> None:
    """A new unversioned write route fails here until someone decides about it."""
    application = create_app(Settings(data_dir=tmp_path / "data"))
    unversioned = {key for key in _write_routes(application) if not is_v1_path(key[1])}
    assert unversioned == set(LEGACY_WRITE_EXEMPTIONS)
    assert not any(is_v1_path(path) for _method, path in LEGACY_WRITE_EXEMPTIONS)


def _request(application: FastAPI, method: str, path: str, route: APIRoute) -> Request:
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "app": application,
            "route": route,
            "path_params": {},
            "state": {},
        }
    )


def test_every_exempt_legacy_route_still_accepts_a_request_without_a_key(
    strict_settings: Settings,
) -> None:
    application = create_app(strict_settings)
    routes = _write_routes(application)
    for method, path in sorted(LEGACY_WRITE_EXEMPTIONS):
        request = _request(application, method, path, routes[(method, path)])
        # Raises for a refused route; returns None for an allowed one.
        enforce_request_policy(strict_settings, request, local_owner())


def test_every_non_exempt_write_needs_a_key(strict_settings: Settings) -> None:
    application = create_app(strict_settings)
    refused = []
    for (method, path), route in sorted(_write_routes(application).items()):
        if (method, path) in LEGACY_WRITE_EXEMPTIONS:
            continue
        with pytest.raises(Problem) as raised:
            enforce_request_policy(
                strict_settings, _request(application, method, path, route), local_owner()
            )
        assert raised.value.status_code == 401
        refused.append(path)
    assert "/api/v1/policy" in refused


def test_strict_mode_over_http(strict_client: TestClient, database: Database) -> None:
    # A non-exempt write without a key is refused...
    blocked = strict_client.put("/api/v1/policy", json={"agent_release": "never"})
    assert blocked.status_code == 401
    assert blocked.json()["code"] == "authentication_required"
    assert blocked.headers["www-authenticate"] == "Bearer"
    # ...reads still work without a key...
    assert strict_client.get("/api/v1/policy").status_code == 200
    assert strict_client.get("/api/settings").status_code == 200
    # ...an exempt legacy write reaches its handler (404 for a missing draft, not 401)...
    legacy = strict_client.patch("/api/drafts/999999", json={"title": "x"})
    assert legacy.status_code == 404
    # ...and a scoped key may write on /api/v1.
    from runway.auth.api_keys import ApiKeyService

    service = ApiKeyService(database, strict_client.app.state.settings)  # type: ignore[attr-defined]
    created = service.create(local_owner(), name="admin", scopes=["workspace:admin"])
    other = service.create(local_owner(), name="old", scopes=["posts:read"])
    revoked = strict_client.delete(
        f"/api/v1/keys/{other.key.id}", headers={"Authorization": f"Bearer {created.secret}"}
    )
    assert revoked.status_code == 200


def test_the_switch_is_off_by_default(settings: Settings, database: Database) -> None:
    assert settings.local_require_key_for_writes is False
    with TestClient(create_app(settings), base_url=BASE_URL) as client:
        assert client.put("/api/v1/policy", json={"agent_release": "never"}).status_code == 200
