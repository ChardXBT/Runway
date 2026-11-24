"""Signed platform webhook routes (spec 04 section 2).

These are the one deliberate exception to "every route receives a principal":
platforms call them without a session, and the platform signature on the raw
body is the authentication. Every POST enforces, in order:

1. the 1 MiB body cap (declared or streamed) — nothing oversized is stored;
2. a per-(provider, source) sliding-window rate limit;
3. signature verification on the raw bytes, before any parsing;
4. durable persistence of the raw event (unverified too, `signature_ok=false`),
   deduplicated by `provider:sha256(body)` for verified deliveries;
5. a `process_webhook` job in the same transaction, for verified new events.

The handler returns immediately; all platform work happens in the worker.
"""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from runway.api.security import host_is_edge
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import PlatformSubscription, WebhookEvent, utcnow
from runway.jobs.queue import JobQueue
from runway.realtime import PROCESS_WEBHOOK_JOB

MAX_BODY_BYTES = 1_048_576  # 1 MiB; real platform payloads are a few KiB
_TIKTOK_SKEW_SECONDS = 300
_RATE_LIMIT_SWEEP_SECONDS = 60.0
_RATE_LIMIT_MAX_KEYS = 10_000
# The direct peers that may name a real client via X-Forwarded-For: only a
# loopback forwarder (Tailscale Funnel) terminating the public edge request.
_LOOPBACK_PEERS = frozenset({"127.0.0.1", "localhost", "::1"})


class _SourceRateLimiter:
    """In-memory sliding window per key; a process-local guard, not the security boundary.

    The source host is spoofable, so the table is bounded two ways: a sweep at
    most once a minute drops every key whose window emptied (a burst of
    distinct sources leaves no residue), and inserting beyond ``max_keys``
    evicts the least-recently-active source.
    """

    def __init__(
        self,
        limit_per_minute: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        max_keys: int = _RATE_LIMIT_MAX_KEYS,
    ) -> None:
        self.limit = max(1, limit_per_minute)
        self._clock = clock
        self._max_keys = max(1, max_keys)
        self._hits: OrderedDict[tuple[str, str], deque[float]] = OrderedDict()
        self._lock = threading.Lock()
        self._next_sweep = 0.0

    def allow(self, key: tuple[str, str], *, now: float | None = None) -> bool:
        moment = self._clock() if now is None else now
        with self._lock:
            if moment >= self._next_sweep:
                self._sweep(moment)
            hits = self._hits.get(key)
            if hits is not None:
                while hits and moment - hits[0] >= 60.0:
                    hits.popleft()
                if hits:
                    self._hits.move_to_end(key)
                else:
                    # No residue: an emptied window frees the key entirely; the
                    # fresh deque lands at the recency end.
                    del self._hits[key]
                    hits = None
            if hits is None:
                hits = deque()
                self._hits[key] = hits
                if len(self._hits) > self._max_keys:
                    # The first key is the least recently active.
                    self._hits.popitem(last=False)
            if len(hits) >= self.limit:
                return False
            hits.append(moment)
            return True

    def _sweep(self, moment: float) -> None:
        """Drop every key whose newest hit has left the window; at most once a minute."""
        self._next_sweep = moment + _RATE_LIMIT_SWEEP_SECONDS
        dead = [key for key, hits in self._hits.items() if not hits or moment - hits[-1] >= 60.0]
        for key in dead:
            del self._hits[key]


async def _capped_body(request: Request) -> bytes | None:
    """The raw body, or None the moment it exceeds the cap.

    A declared Content-Length over the cap is refused without reading; a
    chunked body is refused as soon as the cap is crossed. Either way the
    caller stores nothing.
    """
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES:
                return None
        except ValueError:
            pass  # a malformed header falls back to the stream cap below
    parts: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_BODY_BYTES:
            return None
        parts.append(chunk)
    return b"".join(parts)


def _source(request: Request, edge_host: str | None, edge_port: int | None = None) -> str:
    """The rate-limit source: the direct peer, or the real client behind the edge.

    The left-most X-Forwarded-For entry is trusted only when the request is
    edge traffic AND the direct peer is loopback — the shape of a local
    forwarder like Tailscale Funnel, which terminates the public request and
    connects from 127.0.0.1 with XFF overwritten to the real source. Edge
    traffic is what ``LocalRequestGuard`` marked on the scope (Host is
    client-controlled and can name a loopback address); the Host match stays
    as the fallback for a stack without the guard. In every other case the
    header is trivially spoofable and ignored.
    """
    peer = request.client.host if request.client is not None else "unknown"
    marked_edge = bool(getattr(request.state, "runway_edge", False))
    if peer in _LOOPBACK_PEERS and (
        marked_edge or host_is_edge(request.headers.get("host", ""), edge_host, edge_port)
    ):
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            leftmost = forwarded.split(",", 1)[0].strip()
            if leftmost:
                return leftmost
    return peer


def _verify_websub(secret: str | None, header: str | None, body: bytes) -> bool:
    if not secret or not header:
        return False
    algorithm, _, provided = header.strip().partition("=")
    if algorithm == "sha1":
        digestmod = hashlib.sha1
    elif algorithm == "sha256":
        digestmod = hashlib.sha256
    else:
        return False
    provided = provided.strip()
    if not provided:
        return False
    expected = hmac.new(secret.encode(), body, digestmod).hexdigest()
    # Bytes on both sides: a non-ASCII header can never raise (it just fails).
    return hmac.compare_digest(expected.encode(), provided.encode())


def _verify_hub_signature_256(secret: str | None, header: str | None, body: bytes) -> bool:
    if not secret or not header:
        return False
    algorithm, _, provided = header.strip().partition("=")
    if algorithm != "sha256" or not provided.strip():
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), provided.strip().encode())


def _verify_tiktok(
    secret: str | None, header: str | None, body: bytes, *, now: float | None = None
) -> bool:
    """`TikTok-Signature: t=<unix>,s=<hex>` — HMAC-SHA256 over `t.` + raw body."""
    if not secret or not header:
        return False
    fields: dict[str, str] = {}
    for part in header.split(","):
        key, _, value = part.partition("=")
        fields[key.strip()] = value.strip()
    timestamp_raw = fields.get("t")
    provided = fields.get("s", "").strip()
    if not timestamp_raw or not provided:
        return False
    try:
        timestamp = int(timestamp_raw)
    except ValueError:
        return False
    moment = time.time() if now is None else now
    if abs(moment - timestamp) > _TIKTOK_SKEW_SECONDS:
        return False
    expected = hmac.new(
        secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected.encode(), provided.encode())


def _token_matches(stored: str | None, provided: str) -> bool:
    """Constant-time on bytes, so a non-ASCII path token can never raise."""
    if not stored or not provided:
        return False
    return hmac.compare_digest(stored.encode(), provided.encode())


def _store(
    session: Session,
    queue: JobQueue,
    *,
    provider: str,
    body: bytes,
    signature_ok: bool,
    subscription_id: int | None,
    content_type: str | None,
    source: str,
) -> bool:
    """Persist the event and, for a verified new delivery, its `process_webhook` job.

    Returns False when the delivery is a verified duplicate (the dedupe key
    already exists or lost an insert race): nothing is stored or enqueued.
    """
    dedupe_key = f"{provider}:{hashlib.sha256(body).hexdigest()}" if signature_ok else None
    if dedupe_key is not None and (
        session.scalar(select(WebhookEvent.id).where(WebhookEvent.dedupe_key == dedupe_key))
        is not None
    ):
        return False
    event = WebhookEvent(
        provider=provider,
        subscription_id=subscription_id,
        received_at=utcnow(),
        signature_ok=signature_ok,
        dedupe_key=dedupe_key,
        body=body,
        # SQLite does not enforce VARCHAR lengths; the columns are 100/64.
        content_type=content_type[:100] if content_type is not None else None,
        source=source[:64],
    )
    session.add(event)
    try:
        session.flush()
    except IntegrityError:
        # A concurrent request stored the same verified bytes first.
        session.rollback()
        return False
    if signature_ok:
        queue.enqueue_pending_job(
            session,
            PROCESS_WEBHOOK_JOB,
            idempotency_key=f"webhook:{event.id}",
            payload={"event_id": int(event.id)},
        )
    return True


def _subscription_or_404(
    session: Session, subscription_id: int, callback_token: str
) -> PlatformSubscription:
    """The subscription the callback addresses, or 404.

    The unguessable `callback_token` in the path is what makes a WebSub
    callback unreachable to anyone but the hub we told the URL to: a wrong or
    missing token is the same 404 as an unknown id, and nothing is changed.
    """
    subscription = session.get(PlatformSubscription, subscription_id)
    if subscription is None or not _token_matches(subscription.callback_token, callback_token):
        raise HTTPException(status_code=404)
    return subscription


def build_hooks_router(database: Database, settings: Settings) -> APIRouter:
    router = APIRouter(tags=["hooks"])
    queue = JobQueue(database)
    limiter = _SourceRateLimiter(settings.webhook_rate_limit_per_minute)
    edge_host = settings.public_edge_host
    edge_port = settings.public_edge_port

    @router.get("/hooks/health")
    def hooks_health() -> dict[str, bool]:
        """Liveness for the public edge; returns no data (spec 04 section 6)."""
        return {"ok": True}

    @router.get("/hooks/youtube/websub/{subscription_id}/{callback_token}")
    def youtube_websub_verify(
        subscription_id: int, callback_token: str, request: Request
    ) -> Response:
        mode = request.query_params.get("hub.mode")
        topic = request.query_params.get("hub.topic")
        challenge = request.query_params.get("hub.challenge")
        if topic is None or challenge is None:
            raise HTTPException(status_code=404)
        with database.session() as session:
            subscription = _subscription_or_404(session, subscription_id, callback_token)
            if subscription.topic != topic:
                raise HTTPException(status_code=404)
            if mode == "subscribe":
                if subscription.state not in {"pending", "active"}:
                    raise HTTPException(status_code=404)
                lease_raw = request.query_params.get("hub.lease_seconds")
                try:
                    lease_seconds = int(lease_raw) if lease_raw is not None else None
                except ValueError:
                    raise HTTPException(status_code=404) from None
                now = utcnow()
                subscription.state = "active"
                subscription.lease_seconds = lease_seconds
                subscription.subscribed_at = now
                subscription.expires_at = (
                    now + timedelta(seconds=lease_seconds) if lease_seconds is not None else None
                )
                subscription.last_error = None
                return PlainTextResponse(challenge)
            if mode == "unsubscribe":
                subscription.state = "unsubscribed"
                subscription.expires_at = utcnow()
                return PlainTextResponse(challenge)
            raise HTTPException(status_code=404)

    @router.post("/hooks/youtube/websub/{subscription_id}/{callback_token}")
    async def youtube_websub_delivery(
        subscription_id: int, callback_token: str, request: Request
    ) -> Response:
        body = await _capped_body(request)
        if body is None:
            return JSONResponse({"detail": "body too large"}, status_code=413)
        source = _source(request, edge_host, edge_port)
        if not limiter.allow(("youtube", source)):
            return JSONResponse({"detail": "rate limited"}, status_code=429)
        with database.session() as session:
            subscription = _subscription_or_404(session, subscription_id, callback_token)
            signature_ok = _verify_websub(
                subscription.hub_secret,
                request.headers.get("x-hub-signature"),
                body,
            )
            _store(
                session,
                queue,
                provider="youtube",
                body=body,
                signature_ok=signature_ok,
                subscription_id=subscription.id,
                content_type=request.headers.get("content-type"),
                source=source,
            )
        if signature_ok:
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False}, status_code=403)

    @router.get("/hooks/instagram")
    def instagram_verify(request: Request) -> Response:
        expected = settings.instagram_webhook_verify_token
        token = request.query_params.get("hub.verify_token")
        challenge = request.query_params.get("hub.challenge") or ""
        if expected and token is not None and _token_matches(expected, token):
            return PlainTextResponse(challenge)
        raise HTTPException(status_code=403)

    @router.post("/hooks/instagram")
    async def instagram_delivery(request: Request) -> Response:
        body = await _capped_body(request)
        if body is None:
            return JSONResponse({"detail": "body too large"}, status_code=413)
        source = _source(request, edge_host, edge_port)
        if not limiter.allow(("instagram", source)):
            return JSONResponse({"detail": "rate limited"}, status_code=429)
        signature_ok = _verify_hub_signature_256(
            settings.instagram_app_secret,
            request.headers.get("x-hub-signature-256"),
            body,
        )
        with database.session() as session:
            _store(
                session,
                queue,
                provider="instagram",
                body=body,
                signature_ok=signature_ok,
                subscription_id=None,
                content_type=request.headers.get("content-type"),
                source=source,
            )
        if signature_ok:
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False}, status_code=403)

    @router.post("/hooks/tiktok")
    async def tiktok_delivery(request: Request) -> Response:
        body = await _capped_body(request)
        if body is None:
            return JSONResponse({"detail": "body too large"}, status_code=413)
        source = _source(request, edge_host, edge_port)
        if not limiter.allow(("tiktok", source)):
            return JSONResponse({"detail": "rate limited"}, status_code=429)
        signature_ok = _verify_tiktok(
            settings.tiktok_client_secret,
            request.headers.get("tiktok-signature"),
            body,
        )
        with database.session() as session:
            _store(
                session,
                queue,
                provider="tiktok",
                body=body,
                signature_ok=signature_ok,
                subscription_id=None,
                content_type=request.headers.get("content-type"),
                source=source,
            )
        if signature_ok:
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False}, status_code=403)

    return router
