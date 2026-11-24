"""Meta platform compliance callbacks (spec 05 section 4).

`POST /hooks/meta/deauthorize` - a user removed the app in Facebook settings.
`POST /hooks/meta/data-deletion` - a user asked Meta for their data to be
deleted; Meta requires a JSON `{url, confirmation_code}` answer and a public
status page the URL points at (`GET /hooks/meta/data-deletion/{code}`).

Both carry a form field `signed_request`: "<b64url signature>.<b64url
payload>", signed HMAC-SHA256 with the app secret over the payload segment.
Any configured Meta-family secret may verify (the unified app and the
Instagram app can both own the platform record); a forged or undecodable
delivery is stored with `signature_ok=false` and answered 403, matching the
other receivers.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
from html import escape
from urllib.parse import parse_qs

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import WebhookEvent
from runway.jobs.queue import JobQueue
from runway.realtime.hooks_routes import _capped_body, _source, _SourceRateLimiter, _store

logger = logging.getLogger(__name__)

META_DEAUTHORIZE_PATH = "/hooks/meta/deauthorize"
META_DATA_DELETION_PATH = "/hooks/meta/data-deletion"
META_DATA_DELETION_STATUS_PATH = "/hooks/meta/data-deletion/{code}"
META_DEAUTHORIZE_PROVIDER = "meta-deauthorize"
META_DATA_DELETION_PROVIDER = "meta-data-deletion"
# The confirmation code is the SHA-256 of the signed body - 64 lowercase hex.
_CONFIRMATION_CODE_RE = re.compile(r"[0-9a-f]{64}")


def _app_secrets(settings: Settings) -> list[str]:
    return [
        secret for secret in (settings.meta_app_secret, settings.instagram_app_secret) if secret
    ]


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def verify_signed_request(raw: str, secrets: list[str]) -> dict[str, object] | None:
    """The signed_request payload when any configured secret verifies it."""
    encoded_sig, sep, encoded_payload = raw.partition(".")
    if not sep:
        return None
    try:
        signature = _b64url_decode(encoded_sig)
        payload_bytes = _b64url_decode(encoded_payload)
    except (ValueError, binascii.Error):
        return None
    try:
        payload = json.loads(payload_bytes)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("algorithm") != "HMAC-SHA256":
        return None
    for secret in secrets:
        expected = hmac.new(
            secret.encode("utf-8"), encoded_payload.encode("ascii"), hashlib.sha256
        ).digest()
        if hmac.compare_digest(expected, signature):
            return payload
    return None


def _signed_request_field(body: bytes) -> str | None:
    try:
        fields = parse_qs(body.decode("utf-8"))
    except UnicodeDecodeError:
        return None
    values = fields.get("signed_request")
    if not values:
        return None
    return values[0]


def _status_page(code: str, event: WebhookEvent | None) -> HTMLResponse:
    if event is None:
        title, body = "Data deletion request not found", "Unknown confirmation code."
        status = 404
    elif event.processed_at is None:
        title = "Data deletion request received"
        body = "Your request was received and is queued for processing."
        status = 200
    elif event.error:
        title = "Data deletion request received"
        body = "Your request was received; processing reported an issue. Contact support."
        status = 200
    else:
        title = "Data deletion completed"
        body = "Your data associated with this app has been deleted."
        status = 200
    # `code` is HTML-escaped before it is reflected; an empty code (a path
    # that failed the hex check) is not reflected at all.
    code_line = f"<p>Confirmation code: {escape(code)}</p>" if code else ""
    page = (
        "<!doctype html><html><head><title>Runway data deletion</title></head>"
        f"<body><h1>{title}</h1><p>{body}</p>{code_line}</body></html>"
    )
    return HTMLResponse(page, status_code=status)


def build_meta_hooks_router(database: Database, settings: Settings) -> APIRouter:
    router = APIRouter(tags=["hooks"])
    queue = JobQueue(database)
    limiter = _SourceRateLimiter(settings.webhook_rate_limit_per_minute)
    edge_host = settings.public_edge_host
    edge_port = settings.public_edge_port

    async def _receive(request: Request) -> tuple[bytes | None, str, bool | None]:
        """(body, source, verified) - verified is None when rate limited."""
        body = await _capped_body(request)
        if body is None:
            return None, "", False
        source = _source(request, edge_host, edge_port)
        if not limiter.allow(("meta", source)):
            return body, source, None
        raw = _signed_request_field(body)
        payload = verify_signed_request(raw, _app_secrets(settings)) if raw else None
        return body, source, payload is not None

    @router.post(META_DEAUTHORIZE_PATH)
    async def deauthorize(request: Request) -> JSONResponse:
        body, source, verified = await _receive(request)
        if body is None:
            return JSONResponse({"detail": "body too large"}, status_code=413)
        if verified is None:
            return JSONResponse({"detail": "rate limited"}, status_code=429)
        with database.session() as session:
            _store(
                session,
                queue,
                provider=META_DEAUTHORIZE_PROVIDER,
                body=body,
                signature_ok=verified,
                subscription_id=None,
                content_type=request.headers.get("content-type"),
                source=source,
            )
        if verified:
            return JSONResponse({"ok": True})
        return JSONResponse({"ok": False}, status_code=403)

    @router.post(META_DATA_DELETION_PATH)
    async def data_deletion(request: Request) -> JSONResponse:
        body, source, verified = await _receive(request)
        if body is None:
            return JSONResponse({"detail": "body too large"}, status_code=413)
        if verified is None:
            return JSONResponse({"detail": "rate limited"}, status_code=429)
        with database.session() as session:
            stored = _store(
                session,
                queue,
                provider=META_DATA_DELETION_PROVIDER,
                body=body,
                signature_ok=verified,
                subscription_id=None,
                content_type=request.headers.get("content-type"),
                source=source,
            )
        if not verified:
            return JSONResponse({"ok": False}, status_code=403)
        code = hashlib.sha256(body).hexdigest()
        base = settings.resolved_oauth_redirect_base_url.rstrip("/")
        return JSONResponse(
            {
                "url": f"{base}/hooks/meta/data-deletion/{code}",
                "confirmation_code": code,
                "deduplicated": not stored,
            }
        )

    @router.get(META_DATA_DELETION_STATUS_PATH)
    def data_deletion_status(code: str) -> HTMLResponse:
        # Only an issued-shape code may name a stored request; anything else
        # is answered like an unknown code and never reflected into the page.
        if _CONFIRMATION_CODE_RE.fullmatch(code) is None:
            return _status_page("", None)
        with database.session() as session:
            event = session.scalar(
                select(WebhookEvent).where(
                    WebhookEvent.dedupe_key == f"{META_DATA_DELETION_PROVIDER}:{code}"
                )
            )
        return _status_page(code, event)

    return router
