"""Route walk: every API route must depend on get_principal (spec 01 section 3)."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from fastapi.testclient import TestClient
from starlette.routing import Mount, Route

from runway.api.app import create_app
from runway.auth.principal import get_principal
from runway.config import Settings

# The only routes that may be served without a principal. Each is listed
# explicitly; anything else without the dependency fails this test.
EXEMPT_HEALTH_ROUTES = frozenset({"/health"})
EXEMPT_STATIC_MOUNTS = frozenset({"/media"})


def _calls(dependant: Dependant) -> Iterator[Callable[..., Any]]:
    for dependency in dependant.dependencies:
        if dependency.call is not None:
            yield dependency.call
        yield from _calls(dependency)


def _unguarded_routes(application: FastAPI) -> list[str]:
    missing: list[str] = []
    # iter_route_contexts flattens included routers with their effective
    # (router-level plus route-level) dependencies, as the OpenAPI builder does.
    for context in iter_route_contexts(application.routes):
        route = context.original_route
        if isinstance(route, APIRoute):
            if context.path in EXEMPT_HEALTH_ROUTES:
                continue
            if get_principal not in set(_calls(context.dependant)):
                missing.append(f"{sorted(context.methods or [])} {context.path}")
        elif isinstance(route, Mount):
            if route.path not in EXEMPT_STATIC_MOUNTS:
                missing.append(f"mount {route.path}")
        elif isinstance(route, Route):
            missing.append(f"route {route.path}")
        else:
            missing.append(f"unrecognized route {route!r}")
    return missing


@pytest.fixture()
def local_app(tmp_path: Path) -> FastAPI:
    return create_app(Settings(data_dir=tmp_path / "data"))


def test_every_api_route_requires_a_principal(local_app: FastAPI) -> None:
    api_routes = [
        context
        for context in iter_route_contexts(local_app.routes)
        if isinstance(context.original_route, APIRoute)
    ]
    # Guard against a vacuous pass if routers stop being included.
    assert len(api_routes) > 90
    assert _unguarded_routes(local_app) == []
    # The generated docs are ordinary guarded routes too.
    paths = {context.path for context in api_routes}
    assert {"/openapi.json", "/docs", "/redoc"} <= paths


def test_local_docs_still_answer_without_a_login(local_app: FastAPI) -> None:
    client = TestClient(local_app, base_url="http://127.0.0.1:8000")
    document = client.get("/openapi.json")
    assert document.status_code == 200
    assert "/api/platforms" in document.json()["paths"]
    assert client.get("/docs").status_code == 200
    assert client.get("/redoc").status_code == 200


def test_the_walk_detects_a_route_without_the_dependency(local_app: FastAPI) -> None:
    @local_app.get("/api/unguarded")
    def unguarded() -> dict[str, str]:
        return {}

    assert _unguarded_routes(local_app) == ["['GET'] /api/unguarded"]


def test_hosted_requests_to_api_routes_are_401_but_health_answers(tmp_path: Path) -> None:
    hosted = Settings(data_dir=tmp_path / "data").model_copy(update={"profile": "hosted"})
    application = create_app(hosted)
    # Without the context manager the lifespan (database check) does not run;
    # authentication must refuse before any handler touches the database.
    client = TestClient(application, base_url="http://127.0.0.1:8000")
    for path in ("/api/platforms", "/api/settings", "/api/queue", "/api/drafts"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.json()["code"] == "authentication_required"
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 404
    assert _unguarded_routes(application) == []
