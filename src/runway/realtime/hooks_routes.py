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
from collections import deque
from datetime import timedelta

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse, PlainTextResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import PlatformSubscription, WebhookEvent, utcnow
from runway.jobs.queue import JobQueue
from runway.realtime import PROCESS_WEBHOOK_JOB

MAX_BODY_BYTES = 1_048_576  # 1 MiB; real platform payloads are a few KiB
_TIKTOK_SKEW_SECONDS = 300


class _SourceRateLimiter:
    """In-memory sliding window per key; a process-local guard, not the security boundary."""

    def __init__(self, limit_per_minute: int) -> None:
        self.limit = max(1, limit_per_minute)
        self._hits: dict[tuple[str, str], deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: tuple[str, str], *, now: float | None = None) -> bool:
        moment = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and moment - hits[0] >= 60.0:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(moment)
            return True


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


def _source(request: Request) -> str:
    return request.client.host if request.client is not None else "unknown"


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
    return hmac.compare_digest(expected, provided)


def _verify_hub_signature_256(secret: str | None, header: str | None, body: bytes) -> bool:
    if not secret or not header:
        return False
    algorithm, _, provided = header.strip().partition("=")
    if algorithm != "sha256" or not provided.strip():
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, provided.strip())


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
    return hmac.compare_digest(expected, provided)


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
        content_type=content_type,
        source=source,
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


def build_hooks_router(database: Database, settings: Settings) -> APIRouter:
    router = APIRouter(tags=["hooks"])
    queue = JobQueue(database)
    limiter = _SourceRateLimiter(settings.webhook_rate_limit_per_minute)

    @router.get("/hooks/health")
    def hooks_health() -> dict[str, bool]:
        """Liveness for the public edge; returns no data (spec 04 section 6)."""
        return {"ok": True}

    @router.get("/hooks/youtube/websub/{subscription_id}")
    def youtube_websub_verify(subscription_id: int, request: Request) -> Response:
        mode = request.query_params.get("hub.mode")
        topic = request.query_params.get("hub.topic")
        challenge = request.query_params.get("hub.challenge")
        if topic is None or challenge is None:
            raise HTTPException(status_code=404)
        with database.session() as session:
            subscription = session.get(PlatformSubscription, subscription_id)
            if subscription is None or subscription.topic != topic:
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

    @router.post("/hooks/youtube/websub/{subscription_id}")
    async def youtube_websub_delivery(subscription_id: int, request: Request) -> Response:
        body = await _capped_body(request)
        if body is None:
            return JSONResponse({"detail": "body too large"}, status_code=413)
        source = _source(request)
        if not limiter.allow(("youtube", source)):
            return JSONResponse({"detail": "rate limited"}, status_code=429)
        with database.session() as session:
            subscription = session.get(PlatformSubscription, subscription_id)
            signature_ok = _verify_websub(
                subscription.hub_secret if subscription is not None else None,
                request.headers.get("x-hub-signature"),
                body,
            )
            _store(
                session,
                queue,
                provider="youtube",
                body=body,
                signature_ok=signature_ok,
                subscription_id=subscription.id if subscription is not None else None,
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
        if expected and token is not None and hmac.compare_digest(expected, token):
            return PlainTextResponse(challenge)
        raise HTTPException(status_code=403)

    @router.post("/hooks/instagram")
    async def instagram_delivery(request: Request) -> Response:
        body = await _capped_body(request)
        if body is None:
            return JSONResponse({"detail": "body too large"}, status_code=413)
        source = _source(request)
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
        source = _source(request)
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
