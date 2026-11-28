"""Public edge allow-list (spec 04 section 6, RT2 part A).

Runway binds to loopback and ``LocalRequestGuard`` refuses every other Host.
When ``public_edge_url`` is configured, exactly one edge hostname may reach
the process - and only the webhook and OAuth-callback paths that must be
publicly reachable. Everything else from that host is refused like any
foreign host, and edge requests carrying a session cookie or an
Authorization header are refused outright.

The Host header is never proof of a local caller (recheck-20261123.md
section 4): Tailscale Funnel preserves whatever Host the public client sent,
which can be ``127.0.0.1``. Any forwarding marker - every ``X-Forwarded-*``
header, ``Forwarded``, ``Via``, ``X-Real-IP``, ``True-Client-IP``,
``CF-Connecting-IP`` or ``Tailscale-Funnel-Request`` - makes the request
edge traffic, restricted to the allow-list even when it arrives on a
loopback Host.
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
from runway.db.models import Workspace

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


# ------------------------------------------------------------------ the port


def test_an_edge_url_without_a_port_matches_443_or_none(tmp_path: Path) -> None:
    """No explicit port configured: Host matches on no port or the https
    default only."""
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        public_edge_url="https://edge.example",
    )
    assert settings.public_edge_port is None
    initialize_database(settings)
    application = create_app(settings)
    client = TestClient(application, base_url="https://edge.example")

    assert client.get("/hooks/health").status_code == 200
    assert client.get("/hooks/health", headers={"Host": "edge.example:443"}).status_code == 200
    assert client.get("/hooks/health", headers={"Host": "edge.example:8443"}).status_code == 421


def test_edge_host_matching_requires_an_explicit_configured_port(tmp_path: Path) -> None:
    """An explicit port in public_edge_url must appear verbatim in Host."""
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        public_edge_url="https://edge.example:8443",
    )
    assert settings.public_edge_port == 8443
    assert settings.public_edge_host == "edge.example"
    initialize_database(settings)
    application = create_app(settings)
    client = TestClient(application, base_url="https://edge.example:8443")

    assert client.get("/hooks/health").status_code == 200
    for host in ("edge.example", "edge.example:443", "edge.example:444"):
        assert client.get("/hooks/health", headers={"Host": host}).status_code == 421, host


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
    """Raw-path smuggling: `..`, `//` and any `%` never reach the router.

    Percent signs are banned outright on edge paths - no allow-listed route
    needs them, and `%252e` (double-encoded dot) used to slip past the
    single-decode markers.
    """
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
        b"/hooks/%252e%252e/api/settings",
        b"/hooks/tiktok%252fextra",
        b"/hooks/health%20",
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


# ------------------------------------------------- Host is not proof of local


def test_forwarding_markers_make_a_loopback_request_edge_traffic(
    tmp_path: Path,
) -> None:
    """A forwarded request on a loopback Host is edge traffic, never local.

    Tailscale Funnel preserves the client's Host header - which a hostile
    client can set to ``127.0.0.1`` - and marks the request with
    ``Tailscale-Funnel-Request`` plus ``X-Forwarded-*``. Any of those markers
    means public edge traffic, served only on the allow-listed paths.
    """
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        public_edge_url=EDGE_URL,
    )
    initialize_database(settings)  # webhook stores write before the app runs
    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8000")

    funnel = {"Tailscale-Funnel-Request": "?1", "X-Forwarded-For": "203.0.113.7"}
    assert client.get("/api/settings", headers=funnel).status_code == 421
    assert (
        client.get("/api/settings", headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 421
    )
    # Every forwarding marker marks edge traffic, whatever its value -
    # including every X-Forwarded-* name and the other proxy headers.
    for header in (
        "X-Forwarded-Host",
        "X-Forwarded-Proto",
        "X-Forwarded-Port",
        "X-Forwarded-Server",
        "Forwarded",
        "Via",
        "X-Real-IP",
        "True-Client-IP",
        "CF-Connecting-IP",
    ):
        assert client.get("/api/settings", headers={header: "x"}).status_code == 421, header

    # The allow-listed paths still reach their routes through the funnel
    # (403 is the signature refusal, 200 the health probe - neither a 421).
    assert client.get("/hooks/health", headers=funnel).status_code == 200
    assert client.post("/hooks/tiktok", content=b"{}", headers=funnel).status_code == 403

    # Credentials are refused exactly like on the edge host.
    assert (
        client.get("/hooks/health", headers={**funnel, "Cookie": "runway_session=1"}).status_code
        == 421
    )
    assert (
        client.get("/hooks/health", headers={**funnel, "Authorization": "Bearer x"}).status_code
        == 421
    )

    # Plain loopback traffic keeps full access.
    assert client.get("/api/settings").status_code == 200


def test_edge_traffic_without_the_setting_is_refused_entirely(tmp_path: Path) -> None:
    """Without ``public_edge_url`` even /hooks/ is closed to edge traffic."""
    settings = Settings(data_dir=tmp_path / "data", platform_provider="mock")
    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8000")

    funnel = {"Tailscale-Funnel-Request": "?1", "X-Forwarded-For": "203.0.113.7"}
    assert client.get("/hooks/health", headers=funnel).status_code == 421
    assert client.post("/hooks/tiktok", content=b"{}", headers=funnel).status_code == 421
    assert client.get("/api/settings", headers=funnel).status_code == 421
    # Plain loopback requests are unaffected.
    assert client.get("/api/settings").status_code == 200


def test_edge_requests_are_marked_in_the_scope_state(tmp_path: Path) -> None:
    """The guard marks edge traffic so later code can tell it from local."""
    marked: list[object] = []

    async def sentinel(scope, receive, send):  # type: ignore[no-untyped-def]
        marked.append(scope.get("state", {}).get("runway_edge"))

    guard = LocalRequestGuard(sentinel, allowed_origins=[], edge_host=EDGE_HOST)

    async def receive() -> dict[str, object]:
        return {"type": "http.request"}

    async def send(message: dict[str, object]) -> None:
        pass

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/hooks/health",
        "raw_path": b"/hooks/health",
        "query_string": b"",
        "headers": [
            (b"host", b"127.0.0.1"),
            (b"tailscale-funnel-request", b"?1"),
        ],
    }
    asyncio.run(guard(scope, receive, send))
    assert marked == [True]

    # A plain loopback request is not marked.
    marked.clear()
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/settings",
        "raw_path": b"/api/settings",
        "query_string": b"",
        "headers": [(b"host", b"127.0.0.1")],
    }
    asyncio.run(guard(scope, receive, send))
    assert marked == [None]


def test_edge_requests_never_refresh_the_workspace_seen_at(tmp_path: Path) -> None:
    """``workspaces.last_seen_at`` is the "a human was here" heartbeat; an
    edge request is never one.

    The OAuth callback is the one principal-bearing route the allow-list
    reaches, so it is where a forged edge request could fake human
    activity. It must not move ``last_seen_at`` whether it arrives on the
    edge hostname or on a loopback Host with funnel markers.
    """
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        public_edge_url=EDGE_URL,
    )
    database = initialize_database(settings)
    application = create_app(settings)
    callback = "/api/platforms/youtube/oauth/callback"
    try:
        edge = TestClient(application, base_url=f"https://{EDGE_HOST}")
        assert edge.get(callback, follow_redirects=False).status_code == 303

        local = TestClient(application, base_url="http://127.0.0.1:8000")
        funnel = {"Tailscale-Funnel-Request": "?1", "X-Forwarded-For": "203.0.113.7"}
        assert local.get(callback, headers=funnel, follow_redirects=False).status_code == 303

        with database.session() as session:
            workspace = session.get(Workspace, 1)
            assert workspace is not None and workspace.last_seen_at is None

        # A plain local request to the same route still refreshes it.
        assert local.get(callback, follow_redirects=False).status_code == 303
        with database.session() as session:
            workspace = session.get(Workspace, 1)
            assert workspace is not None and workspace.last_seen_at is not None
    finally:
        database.engine.dispose()
        application.state.database.engine.dispose()


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


def test_a_fqdn_loopback_host_with_a_trailing_dot_is_still_loopback(tmp_path: Path) -> None:
    """``localhost.`` is the absolute-DNS spelling of ``localhost`` - some
    clients send it - so the loopback comparison strips a trailing dot."""
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
    )
    client = TestClient(create_app(settings), base_url="http://localhost:8000")
    assert client.get("/health", headers={"Host": "localhost."}).status_code == 200
    assert client.get("/api/settings", headers={"Host": "localhost."}).status_code == 200
