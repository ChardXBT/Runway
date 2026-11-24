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
# Raw-path smuggling markers: dot segments, double slashes and their encoded
# forms must never reach the router via the edge host.
_EDGE_RAW_BANNED = (b"..", b"//", b"%2e", b"%2f")


def loopback_origins(port: int) -> list[str]:
    return [f"http://{hostname}:{port}" for hostname in sorted(LOOPBACK_HOSTNAMES)]


def _hostname(host_header: str) -> str:
    host = host_header.strip().casefold()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def host_is_edge(host_header: str, edge_host: str | None) -> bool:
    """True when a Host header names the configured public edge hostname.

    Shared by the guard and the webhook rate-limit source so both normalize a
    host the same way (case-folded, bracketed IPv6 kept, one port dropped).
    """
    return edge_host is not None and _hostname(host_header) == _hostname(edge_host)


class LocalRequestGuard:
    def __init__(
        self,
        app: ASGIApp,
        *,
        allowed_origins: Iterable[str],
        edge_host: str | None = None,
    ) -> None:
        self.app = app
        self.allowed_origins = frozenset(origin.casefold() for origin in allowed_origins)
        # Matched on the DNS name only: the port in a Host header does not
        # confer trust, and the edge path check still applies after a match.
        self.edge_host = _hostname(edge_host) if edge_host else None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        host = _hostname(headers.get("host", ""))
        if host in LOOPBACK_HOSTNAMES:
            pass
        elif self.edge_host is not None and host == self.edge_host:
            if not self._edge_allowed(scope, headers):
                await self._reject(scope, receive, send, 421, "Runway only serves loopback hosts.")
                return
        else:
            await self._reject(scope, receive, send, 421, "Runway only serves loopback hosts.")
            return
        if scope.get("method", "GET") not in SAFE_METHODS or scope["type"] == "websocket":
            origin = headers.get("origin")
            if origin is not None and origin.casefold() not in self.allowed_origins:
                await self._reject(scope, receive, send, 403, "Cross-origin request refused.")
                return
            if origin is None and headers.get("sec-fetch-site") in {"cross-site", "same-site"}:
                await self._reject(scope, receive, send, 403, "Cross-site request refused.")
                return
        await self.app(scope, receive, send)

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
