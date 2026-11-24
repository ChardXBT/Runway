"""Public edge allow-list (spec 04 section 6, RT2 part A).

Runway binds to loopback and ``LocalRequestGuard`` refuses every other Host.
When ``public_edge_url`` is configured, exactly one edge hostname may reach
the process - and only the webhook and OAuth-callback paths that must be
publicly reachable. Everything else from that host is refused like any
foreign host, and edge requests carrying a session cookie or an
Authorization header are refused outright.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from runway.api.app import create_app
from runway.api.security import LocalRequestGuard
from runway.config import Settings
from runway.db import initialize_database

EDGE_HOST = "edge.runway.example"
EDGE_URL = f"https://{EDGE_HOST}"


def _client(tmp_path: Path, **overrides: object) -> TestClient:
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        **overrides,  # type: ignore[arg-type]
    )
    initialize_database(settings)  # webhook stores write before the app runs
    return TestClient(create_app(settings), base_url=f"https://{EDGE_HOST}")


# ----------------------------------------------------------------- settings


def test_public_edge_url_must_be_https_host_only(tmp_path: Path) -> None:
    good = Settings(
        data_dir=tmp_path / "data", platform_provider="mock", public_edge_url=EDGE_URL + "/"
    )
    assert good.public_edge_url == EDGE_URL
    for bad in (
        "http://edge.runway.example",  # scheme must be https
        "https://edge.runway.example/hooks",  # no path beyond "/"
        "https://edge.runway.example/?x=1",  # no query
        "https://user@edge.runway.example",  # no userinfo
        "https://user:pw@edge.runway.example",
        "edge.runway.example",  # no scheme
        "https://edge.runway.example#frag",
    ):
        with pytest.raises(ValidationError):
            Settings(data_dir=tmp_path / "data", platform_provider="mock", public_edge_url=bad)


# ------------------------------------------------------------- the allow-list


def test_edge_host_reaches_hooks_and_oauth_callback_but_not_the_api(tmp_path: Path) -> None:
    client = _client(tmp_path, public_edge_url=EDGE_URL)

    assert client.get("/hooks/health").status_code == 200
    assert client.get("/hooks/health").json() == {"ok": True}

    # Signature verification refuses the forged body, but the 403 proves the
    # request reached the route instead of dying at the guard with 421.
    tiktok = client.post("/hooks/tiktok", content=b"{}")
    assert tiktok.status_code == 403

    instagram = client.get("/hooks/instagram")
    assert instagram.status_code != 421

    # The OAuth callback redirects on missing params; again, not a 421.
    callback = client.get(
        "/api/platforms/youtube/oauth/callback",
        follow_redirects=False,
    )
    assert callback.status_code == 303

    # Everything else stays refused, exactly like a foreign host.
    for path in (
        "/api/settings",
        "/api/platforms",
        "/health",
        "/hooks",  # the prefix must end with a slash
        "/api/platforms/youtube/oauth/callbackevil",
        "/api/platforms/youtube/oauth/callback/extra",
        "/api/v1/events/stream",
    ):
        response = client.get(path, follow_redirects=False)
        assert response.status_code == 421, path


def test_edge_requests_with_credentials_are_refused(tmp_path: Path) -> None:
    client = _client(tmp_path, public_edge_url=EDGE_URL)

    cookie = client.get("/hooks/health", headers={"Cookie": "runway_session=abc"})
    assert cookie.status_code == 421
    bearer = client.get("/hooks/health", headers={"Authorization": "Bearer abc"})
    assert bearer.status_code == 421


def test_edge_paths_with_dot_segments_or_encoded_slashes_are_refused(tmp_path: Path) -> None:
    """Raw-path smuggling: `..`, `//`, `%2e`, `%2f` never reach the router."""
    reached: list[str] = []

    async def sentinel(scope, receive, send):  # type: ignore[no-untyped-def]
        reached.append(scope["path"])

    guard = LocalRequestGuard(sentinel, allowed_origins=[], edge_host=EDGE_HOST)

    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return {"type": "http.request"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    for raw in (
        b"/hooks/../api/settings",
        b"/hooks/%2e%2e/api/settings",
        b"/hooks/%2E%2E/api/settings",
        b"/%2e%2e/hooks/health",
        b"/hooks//tiktok",
        b"/hooks/%2f%2f/tiktok",
        b"/api/platforms/%2e%2e/oauth/callback",
    ):
        sent.clear()
        scope = {
            "type": "http",
            "method": "GET",
            "path": raw.decode(),
            "raw_path": raw,
            "query_string": b"",
            "headers": [(b"host", EDGE_HOST.encode())],
        }
        asyncio.run(guard(scope, receive, send))
        response = next(m for m in sent if m["type"] == "http.response.start")
        assert response["status"] == 421, raw
    assert reached == []


def test_the_edge_host_without_the_setting_is_a_foreign_host(tmp_path: Path) -> None:
    client = _client(tmp_path)  # no public_edge_url
    assert client.get("/hooks/health").status_code == 421
    assert client.get("/api/platforms/youtube/oauth/callback").status_code == 421


def test_loopback_behaviour_is_unchanged(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        public_edge_url=EDGE_URL,
    )
    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8000")
    assert client.get("/health").status_code == 200
    assert client.get("/hooks/health").json() == {"ok": True}
    # Loopback requests may still carry credentials - the refusal applies to
    # the edge host only.
    assert client.get("/api/settings", headers={"Authorization": "Bearer x"}).status_code == 200
