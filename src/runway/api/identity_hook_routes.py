"""`POST /hooks/identity/clerk`: Clerk's user, organization and membership webhooks.

Authenticated by the Svix signature, not by a principal (the route walk in
tests/integration/test_route_principal.py lists it as a signature-authenticated
exemption). A replay of a delivery id is answered 200 without being applied
again; an event older than one already applied for the same subject is
recorded as stale. Mounted only in the hosted profile with Clerk configured.
"""

from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Request
from sqlalchemy.exc import IntegrityError
from starlette.concurrency import run_in_threadpool

from runway.auth.problems import Problem
from runway.config import Settings
from runway.db.base import Database
from runway.identity import svix
from runway.identity.clerk import PROVIDER_NAME as CLERK
from runway.identity.clerk_webhooks import WebhookPayloadInvalid, parse_clerk_event
from runway.identity.events import ParsedDelivery
from runway.identity.mirror import IdentityMirror

logger = logging.getLogger(__name__)

CLERK_HOOK_PATH = "/hooks/identity/clerk"
# Clerk event bodies are a few kilobytes; refuse anything far larger unread.
MAXIMUM_BODY_BYTES = 256 * 1024


async def _read_body(request: Request) -> bytes:
    declared = request.headers.get("content-length")
    if declared is not None and declared.isdigit() and int(declared) > MAXIMUM_BODY_BYTES:
        raise Problem(413, "payload_too_large", "Payload too large", "the delivery is too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAXIMUM_BODY_BYTES:
            raise Problem(
                413, "payload_too_large", "Payload too large", "the delivery is too large"
            )
    return bytes(body)


def _apply(
    database: Database, mirror: IdentityMirror, parsed: ParsedDelivery, delivery_id: str
) -> str:
    try:
        with database.session() as session:
            return mirror.apply(session, parsed, delivery_id)
    except IntegrityError:
        # A concurrent delivery of the same id won the unique insert; or two
        # events raced on a new user or member row. Either way, re-check.
        with database.session() as session:
            return mirror.apply(session, parsed, delivery_id)


def build_identity_hook_router(database: Database, settings: Settings) -> APIRouter:
    router = APIRouter(tags=["identity"])
    mirror = IdentityMirror(CLERK, default_role=settings.hosted_default_member_role)

    @router.post(CLERK_HOOK_PATH, include_in_schema=False)
    async def clerk_webhook(request: Request) -> dict[str, object]:
        secrets = settings.clerk_webhook_secrets
        if not secrets:
            raise Problem(
                503,
                "webhook_not_configured",
                "Webhook not configured",
                "RUNWAY_CLERK_WEBHOOK_SECRET is not set",
            )
        body = await _read_body(request)
        try:
            delivery = svix.verify(secrets, request.headers, body)
        except (svix.WebhookSignatureInvalid, ValueError) as exc:
            logger.warning(
                "identity webhook signature refused: %s",
                exc,
                extra={"event_type": "identity_webhook_signature_failed", "provider": CLERK},
            )
            raise Problem(
                401,
                "webhook_signature_invalid",
                "Invalid webhook signature",
                "the delivery is not signed with this endpoint's secret",
            ) from None
        try:
            parsed = parse_clerk_event(json.loads(body))
        except (UnicodeDecodeError, json.JSONDecodeError, WebhookPayloadInvalid) as exc:
            raise Problem(
                400,
                "webhook_payload_invalid",
                "Unreadable webhook payload",
                str(exc) if isinstance(exc, WebhookPayloadInvalid) else "the body is not JSON",
            ) from None
        outcome = await run_in_threadpool(_apply, database, mirror, parsed, delivery.delivery_id)
        logger.info(
            "identity webhook %s %s",
            parsed.event_type,
            outcome,
            extra={"event_type": "identity_webhook", "provider": CLERK, "outcome": outcome},
        )
        return {"received": True, "event_type": parsed.event_type, "outcome": outcome}

    return router
