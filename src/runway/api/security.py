"""Request guard for the loopback-only API.

Binding to 127.0.0.1 keeps other machines out, but a web page in the operator's own
browser can still reach the API. Two checks close that gap:

* DNS rebinding: an attacker hostname that resolves to 127.0.0.1 arrives with its own
  Host header, so only loopback Host names are served.
* Cross-site requests: a form or ``no-cors`` fetch needs no CORS preflight, so a
  state-changing request is refused when the browser reports a foreign Origin or a
  cross-site fetch. Clients that send neither (the CLI, curl, the Next.js server) are
  unaffected.
"""

from __future__ import annotations

from collections.abc import Iterable

from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

LOOPBACK_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "[::1]"})
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def loopback_origins(port: int) -> list[str]:
    return [f"http://{hostname}:{port}" for hostname in sorted(LOOPBACK_HOSTNAMES)]


def _hostname(host_header: str) -> str:
    host = host_header.strip().casefold()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class LocalRequestGuard:
    def __init__(self, app: ASGIApp, *, allowed_origins: Iterable[str]) -> None:
        self.app = app
        self.allowed_origins = frozenset(origin.casefold() for origin in allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        if _hostname(headers.get("host", "")) not in LOOPBACK_HOSTNAMES:
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

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, status: int, detail: str) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        await JSONResponse({"detail": detail}, status_code=status)(scope, receive, send)
