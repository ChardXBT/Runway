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
from identity_support import WEBHOOK_SECRET
from pydantic import SecretStr
from starlette.routing import Mount, Route

from runway.api.app import create_app
from runway.api.v1.router import V1Route
from runway.auth.principal import get_principal
from runway.config import Settings

# hosted resolves the database token store (spec 05 section 3): the API and
# worker validate the keyring at startup.
_TOKEN_KEYRING = "v1:" + "a" * 43 + "="

# The only routes that may be served without a principal. Each is listed
# explicitly; anything else without the dependency fails this test.
EXEMPT_HEALTH_ROUTES = frozenset({"/health"})
# Webhook receivers authenticated by the sender's signature, not by a
# principal (hosted profile only). Each verifies its own signature first.
EXEMPT_SIGNATURE_ROUTES = frozenset({"/hooks/identity/clerk"})
EXEMPT_STATIC_MOUNTS = frozenset({"/media"})
# Authenticated by platform signatures; no session, no workspace (spec 04 section 2).
EXEMPT_SIGNED_WEBHOOKS = frozenset(
    {
        # liveness for the public edge; returns no data
        "/hooks/health",
        "/hooks/youtube/websub/{subscription_id}/{callback_token}",
        "/hooks/instagram",
        "/hooks/tiktok",
    }
)


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
            if (
                context.path in EXEMPT_HEALTH_ROUTES | EXEMPT_SIGNATURE_ROUTES
                or context.path in EXEMPT_SIGNED_WEBHOOKS
            ):
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
    # Every /api/v1 route comes from build_v1_router.
    v1_routes = [context for context in api_routes if (context.path or "").startswith("/api/v1")]
    assert "/api/v1/events/stream" in {context.path for context in v1_routes}
    assert all(isinstance(context.original_route, V1Route) for context in v1_routes)


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
    hosted.token_keys = SecretStr(_TOKEN_KEYRING)
    application = create_app(hosted)
    # Without the context manager the lifespan (database check) does not run;
    # authentication must refuse before any handler touches the database.
    client = TestClient(application, base_url="http://127.0.0.1:8000")
    for path in ("/api/settings", "/api/v1/workspace", "/api/v1/workspace/members"):
        response = client.get(path)
        assert response.status_code == 401, path
        assert response.json()["code"] == "authentication_required"
    # Local-only surfaces do not exist in hosted (spec 01 section 7), nor do
    # the platform routes, which are not scoped to a workspace yet.
    for path in ("/api/queue", "/api/platforms", "/api/drafts", "/api/inbox"):
        assert client.get(path).status_code == 404, path
    assert client.get("/health").status_code == 200
    assert client.get("/openapi.json").status_code == 404
    assert _unguarded_routes(application) == []


def test_hosted_clerk_app_exempts_only_its_signed_webhook(tmp_path: Path) -> None:
    hosted = Settings(
        data_dir=tmp_path / "data",
        clerk_issuer="https://clerk.runway.test",
        web_origins="https://app.runway.test",
        clerk_webhook_secret=WEBHOOK_SECRET,
    ).model_copy(update={"profile": "hosted", "identity_provider": "clerk"})
    hosted.token_keys = SecretStr(_TOKEN_KEYRING)
    application = create_app(hosted)
    paths = {context.path for context in iter_route_contexts(application.routes)}
    assert paths >= EXEMPT_SIGNATURE_ROUTES
    assert _unguarded_routes(application) == []


# Every route the hosted API serves, and why each is safe there. Adding a
# router to hosted must add it here, with its tenancy argument.
HOSTED_ROUTES = {
    # Liveness probe: product name, version and switches; no tenant data.
    ("GET", "/health"),
    # Product switches only (hosted_settings_view), behind get_principal.
    ("GET", "/api/settings"),
    # Workspace router: every query is filtered by principal.workspace_id and
    # role changes require workspace:admin.
    ("GET", "/api/v1/workspace"),
    ("GET", "/api/v1/workspace/members"),
    ("PATCH", "/api/v1/workspace/members/{user_id}"),
    # Event stream: every query is filtered by principal.workspace_id.
    ("GET", "/api/v1/events/stream"),
    # Clerk webhook: authenticated by the Svix signature, not by a principal.
    ("POST", "/hooks/identity/clerk"),
}


def test_hosted_serves_exactly_the_listed_routes(tmp_path: Path) -> None:
    hosted = Settings(
        data_dir=tmp_path / "data",
        clerk_issuer="https://clerk.runway.test",
        web_origins="https://app.runway.test",
        clerk_webhook_secret=WEBHOOK_SECRET,
    ).model_copy(update={"profile": "hosted", "identity_provider": "clerk"})
    hosted.token_keys = SecretStr(_TOKEN_KEYRING)
    application = create_app(hosted)
    served = {
        (method, context.path)
        for context in iter_route_contexts(application.routes)
        for method in (context.methods or set())
        if method != "HEAD"
    }
    assert served == HOSTED_ROUTES
    assert not [route for route in application.routes if isinstance(route, Mount)]
