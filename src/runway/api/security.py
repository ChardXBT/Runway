"""Request guard for the loopback-only API.

Binding to 127.0.0.1 keeps other machines out, but a web page in the operator's own
browser can still reach the API. Two checks close that gap:

* DNS rebinding: an attacker hostname that resolves to 127.0.0.1 arrives with its own
  Host header, so only loopback Host names are served.
* Cross-site requests: a form or ``no-cors`` fetch needs no CORS preflight, so a
  state-changing request is refused when the browser reports a foreign Origin or a
  cross-site fetch. Clients that send neither (the CLI, curl, the Next.js server) are
  unaffected.

When ``public_edge_url`` is configured (spec 04 section 6), exactly one edge
hostname is also served - and only the paths that must be publicly reachable:
``/hooks/`` for platform webhooks and the OAuth callback. Anything else from
that host gets the same refusal as every foreign host.

The Host header is never proof that a request is local (recheck-20261123.md
section 4): Tailscale Funnel preserves whatever Host the public client sent,
which can be a loopback name. Edge traffic is therefore recognized by what a
forwarder must add - ``Tailscale-Funnel-Request``, ``X-Forwarded-For``,
``X-Forwarded-Host``, ``X-Forwarded-Proto``, ``Forwarded`` or ``X-Real-IP`` -
or by the configured edge hostname itself, and it is served only on the
allow-listed paths (with cookies and Authorization refused). Without
``public_edge_url`` such traffic is refused entirely.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# The only path families the public edge may reach (spec 04 section 6).
EDGE_HOOKS_PREFIX = "/hooks/"
EDGE_OAUTH_CALLBACK = re.compile(r"^/api/platforms/[^/]+/oauth/callback$")
# Raw-path smuggling markers: dot segments and double slashes must never
# reach the router via the edge host, and no allow-listed route needs a
# percent sign - banning "%" outright also kills multiply-encoded forms
# like %252e that a single decode would re-arm.
_EDGE_RAW_BANNED = (b"..", b"//", b"%")
# A request carrying any of these arrived through a forwarder: it is edge
# traffic no matter what Host it claims (recheck-20261123.md section 4 -
# Tailscale Funnel preserves the client Host, marks the request with
# Tailscale-Funnel-Request and overwrites X-Forwarded-For).
EDGE_MARKER_HEADERS: frozenset[str] = frozenset(
    {
        "tailscale-funnel-request",
        "x-forwarded-for",
        "x-forwarded-host",
        "x-forwarded-proto",
        "forwarded",
        "x-real-ip",
    }
)


def loopback_origins(port: int) -> list[str]:
    return [f"http://{hostname}:{port}" for hostname in sorted(LOOPBACK_HOSTNAMES)]


def cross_site_refusal(headers: Headers, allowed_origins: frozenset[str]) -> str | None:
    """Why a state-changing browser request must be refused, or None.

    Shared by the local guard and the hosted guard (runway.hosted.middleware).
    `allowed_origins` must already be case-folded.
    """
    origin = headers.get("origin")
    if origin is not None and origin.casefold() not in allowed_origins:
        return "Cross-origin request refused."
    if origin is None and headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
        return "Cross-site request refused."
    return None


def _hostname(host_header: str) -> str:
    host = host_header.strip().casefold()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _host_port(host_header: str) -> tuple[str, int | None]:
    """Split a Host header into (hostname, port); the port is None when absent
    or non-numeric, and bracketed IPv6 literals keep their brackets."""
    host = host_header.strip().casefold()
    if host.startswith("["):
        name, sep, rest = host.partition("]")
        if not sep:
            return host, None
        port = rest[1:] if rest.startswith(":") else ""
        return name + "]", int(port) if port.isdigit() else None
    if host.count(":") == 1:
        name, _, port = host.partition(":")
        return name, int(port) if port.isdigit() else None
    return host, None


def host_is_edge(host_header: str, edge_host: str | None, edge_port: int | None = None) -> bool:
    """True when a Host header names the configured public edge host.

    Shared by the guard and the webhook rate-limit source so both normalize a
    host the same way (case-folded, bracketed IPv6 kept). An explicit port in
    ``public_edge_url`` must appear verbatim in the Host; without one the
    hostname matches on the https default (443 or no port).
    """
    if edge_host is None:
        return False
    hostname, port = _host_port(host_header)
    if hostname != _hostname(edge_host):
        return False
    if edge_port is not None:
        return port == edge_port
    return port in (None, 443)


class LocalRequestGuard:
    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_origins: Iterable[str],
        edge_host: str | None = None,
        edge_port: int | None = None,
    ) -> None:
        self.app = app
        self.allowed_origins = frozenset(origin.casefold() for origin in allowed_origins)
        # The configured edge port pins Host matching; without one the edge
        # hostname matches only on the https default (443 or no port). The
        # edge path check still applies after a match.
        self.edge_host = _hostname(edge_host) if edge_host else None
        self.edge_port = edge_port

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if self._is_edge(headers):
            # Marked before the allow-list decision so served edge requests
            # stay distinguishable from local ones downstream (the webhook
            # rate limit keys on XFF only for these, and they must never
            # refresh workspaces.last_seen_at).
            scope.setdefault("state", {})["runway_edge"] = True
            if self.edge_host is None or not self._edge_allowed(scope, headers):
                await self._reject(scope, receive, send, 421, "Runway only serves loopback hosts.")
                return
        elif _hostname(headers.get("host", "")) in LOOPBACK_HOSTNAMES:
            pass
        else:
            await self._reject(scope, receive, send, 421, "Runway only serves loopback hosts.")
            return
        if scope.get("method", "GET") not in SAFE_METHODS or scope["type"] == "websocket":
            refusal = cross_site_refusal(headers, self.allowed_origins)
            if refusal is not None:
                await self._reject(scope, receive, send, 403, refusal)
                return
        await self.app(scope, receive, send)

    def _is_edge(self, headers: Headers) -> bool:
        """True when the request is public edge traffic, whatever Host claims.

        The client controls Host (the funnel preserves it), so the edge is
        recognized by the forwarding markers a reverse proxy adds - or by
        the configured edge hostname itself, which covers a hypothetical
        marker-less forwarder.
        """
        if any(marker in headers for marker in EDGE_MARKER_HEADERS):
            return True
        return host_is_edge(headers.get("host", ""), self.edge_host, self.edge_port)

    def _edge_allowed(self, scope: Scope, headers: Headers) -> bool:
        """True only for the webhooks and OAuth-callback paths, credential-free."""
        raw = scope.get("raw_path") or str(scope.get("path", "")).encode()
        lowered = raw.lower()
        if any(marker in lowered for marker in _EDGE_RAW_BANNED):
            return False
        path = str(scope.get("path", ""))
        if ".." in path or "//" in path:
            return False
        if not (path.startswith(EDGE_HOOKS_PREFIX) or EDGE_OAUTH_CALLBACK.match(path)):
            return False
        # The allowed routes authenticate by platform signature or OAuth state;
        # a session cookie or bearer token on this host means a browser (or a
        # confused client) is talking to the edge surface - refuse it.
        return headers.get("cookie") is None and headers.get("authorization") is None

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, status: int, detail: str) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)
