"""ASGI middleware for the hosted profile (spec 05 sections 2 and 6).

* `HostedRequestGuard`: the local guard's cross-site check, against the
  configured web origins instead of loopback; and a state-changing request
  authenticated by the session cookie must carry a matching `Origin`.
* `ClientRateLimit`: per-client-IP token bucket, before authentication, so
  failed sign-ins and forged tokens are throttled too.
* `SecurityHeaders`: HSTS, no-sniff, frame denial and no-store on every
  response.

The hosted API sits behind a TLS-terminating reverse proxy; `runway serve`
starts uvicorn with proxy headers trusted only from `RUNWAY_FORWARDED_ALLOW_IPS`,
so `scope["client"]` is the real client address.
"""

from __future__ import annotations

from collections.abc import Iterable

from starlette.datastructures import Headers, MutableHeaders
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from runway.api.security import SAFE_METHODS, cross_site_refusal
from runway.auth.problems import PROBLEM_MEDIA_TYPE, Problem
from runway.hosted.ratelimit import RateLimit, RateLimiter, rate_limited
from runway.identity.base import presented_credential

SECURITY_HEADERS: tuple[tuple[str, str], ...] = (
    ("strict-transport-security", "max-age=63072000; includeSubDomains"),
    ("x-content-type-options", "nosniff"),
    ("x-frame-options", "DENY"),
    ("referrer-policy", "no-referrer"),
    ("content-security-policy", "default-src 'none'; frame-ancestors 'none'"),
)


async def send_problem(scope: Scope, receive: Receive, send: Send, problem: Problem) -> None:
    response = JSONResponse(
        problem.body(),
        status_code=problem.status_code,
        headers=problem.headers,
        media_type=PROBLEM_MEDIA_TYPE,
    )
    await response(scope, receive, send)


class HostedRequestGuard:
    def __init__(self, app: ASGIApp, *, allowed_origins: Iterable[str]) -> None:
        self.app = app
        self.allowed_origins = frozenset(origin.casefold() for origin in allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return
        if scope["type"] == "websocket" or scope.get("method", "GET") not in SAFE_METHODS:
            headers = Headers(scope=scope)
            problem = self._refusal(headers)
            if problem is not None:
                if scope["type"] == "websocket":
                    await send({"type": "websocket.close", "code": 1008})
                    return
                await send_problem(scope, receive, send, problem)
                return
        await self.app(scope, receive, send)

    def _refusal(self, headers: Headers) -> Problem | None:
        refusal = cross_site_refusal(headers, self.allowed_origins)
        if refusal is not None:
            return Problem(403, "cross_origin_refused", "Cross-origin request refused", refusal)
        presented = presented_credential(headers)
        if presented is not None and presented[0] == "cookie":
            origin = headers.get("origin")
            if origin is None or origin.casefold() not in self.allowed_origins:
                return Problem(
                    403,
                    "origin_required",
                    "Origin required",
                    "a state-changing request signed in by cookie must come from the web app",
                )
        return None


class ClientRateLimit:
    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: RateLimiter,
        limit: RateLimit,
        exempt_paths: Iterable[str] = ("/health",),
    ) -> None:
        self.app = app
        self.limiter = limiter
        self.limit = limit
        self.exempt_paths = frozenset(exempt_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") in self.exempt_paths:
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        address = client[0] if client else "unknown"
        decision = self.limiter.consume(f"ip:{address}", self.limit)
        if not decision.allowed:
            problem = rate_limited(decision, "too many requests from this address; slow down")
            await send_problem(scope, receive, send, problem)
            return
        await self.app(scope, receive, send)


class SecurityHeaders:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in SECURITY_HEADERS:
                    headers.setdefault(name, value)
                headers.setdefault("cache-control", "no-store")
            await send(message)

        await self.app(scope, receive, send_with_headers)
