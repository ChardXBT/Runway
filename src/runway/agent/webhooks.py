"""Outbound webhooks for agents (spec 06 section 8).

A workspace registers an endpoint (URL, event filter); Runway generates a
signing secret, shows it once, and stores it Fernet-encrypted with the token
store's key. The worker then:

1. fans new events out into `webhook_deliveries` rows (the endpoint's
   `cursor_event_id` remembers how far it got; a new endpoint starts at the
   newest event, so nothing old is replayed);
2. enqueues one `deliver_webhook` job per due delivery attempt;
3. POSTs the event as JSON, signed
   `Runway-Signature: t=<unix seconds>,s=<hex HMAC-SHA256 over "<t>.<body>">`,
   with no redirects followed;
4. on failure retries with exponential backoff (30 s doubling, at most 1 h
   apart) until the delivery is 24 hours old, then marks it `failed`;
5. disables the endpoint after sustained failure: failing without a success
   for 24 hours and at least `DISABLE_AFTER_FAILURES` attempts in a row.

SSRF guard: a delivery never goes to a loopback, private, link-local or other
non-global address. Every address the host resolves to is checked right
before each attempt. In the `hosted` profile there is no exception; in
`local` the owner may allow private targets with
`RUNWAY_WEBHOOK_ALLOW_PRIVATE_TARGETS=true` (for a receiver on the same
machine or LAN).
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from runway.api.v1.pagination import CursorPage, CursorParams, paginate
from runway.auth.principal import WORKSPACE_ADMIN, Principal, require
from runway.auth.problems import Problem, ProblemCode
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import EventRecord, WebhookDelivery, WebhookEndpoint, utcnow
from runway.events.log import Event
from runway.events.types import EVENT_TYPES
from runway.events.visibility import event_visible
from runway.jobs.queue import Job, JobQueue
from runway.platforms.tokens import TokenStore

logger = logging.getLogger(__name__)

DELIVER_WEBHOOK_JOB = "deliver_webhook"
SIGNATURE_HEADER = "Runway-Signature"
SECRET_PREFIX = "whsec_"
USER_AGENT = "Runway-Webhooks/1"

RETRY_WINDOW = timedelta(hours=24)
FIRST_RETRY = timedelta(seconds=30)
MAX_RETRY_GAP = timedelta(hours=1)
DISABLE_AFTER = timedelta(hours=24)
DISABLE_AFTER_FAILURES = 10
# How often the worker looks for new events when nothing else is due.
FAN_OUT_INTERVAL = timedelta(seconds=10)
FAN_OUT_BATCH = 500
DUE_BATCH = 100
MAX_URL_LENGTH = 2000

EndpointState = Literal["active", "disabled"]
Resolver = Callable[[str, int], Sequence[str]]


# ------------------------------------------------------------------ schemas


class WebhookCreate(BaseModel):
    url: str = Field(min_length=8, max_length=MAX_URL_LENGTH)
    # Empty: every event type.
    event_types: list[str] = Field(default_factory=list, max_length=len(EVENT_TYPES))
    description: str | None = Field(default=None, max_length=200)


class WebhookPatch(BaseModel):
    state: EndpointState | None = None
    event_types: list[str] | None = Field(default=None, max_length=len(EVENT_TYPES))
    description: str | None = Field(default=None, max_length=200)


class WebhookView(BaseModel):
    id: int
    url: str
    event_types: list[str]
    state: str
    description: str | None
    created_by: str | None
    created_at: datetime
    consecutive_failures: int
    failing_since: datetime | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error: str | None
    disabled_at: datetime | None
    disabled_reason: str | None


class CreatedWebhook(WebhookView):
    # Shown once; Runway keeps only the encrypted value.
    secret: str


class DeliveryView(BaseModel):
    id: int
    endpoint_id: int
    event_id: int
    event_type: str
    state: str
    attempts: int
    next_attempt_at: datetime
    last_status_code: int | None
    last_error: str | None
    created_at: datetime
    delivered_at: datetime | None


# ------------------------------------------------------------------ helpers


def _problem(code: ProblemCode, detail: str) -> Problem:
    return Problem.of(code, detail)


def sign(secret: str, timestamp: int, body: bytes) -> str:
    """The `Runway-Signature` header value for `body` sent at `timestamp`."""
    message = str(timestamp).encode("ascii") + b"." + body
    digest = hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()
    return f"t={timestamp},s={digest}"


def verify_signature(secret: str, header: str, body: bytes) -> bool:
    """Receiver-side check (documented in docs/AGENT_API.md; used by the tests)."""
    parts = dict(part.split("=", 1) for part in header.split(",") if "=" in part)
    timestamp = parts.get("t", "")
    if not timestamp.isdigit():
        return False
    expected = sign(secret, int(timestamp), body)
    return hmac.compare_digest(expected, header)


def retry_delay(attempts: int) -> timedelta:
    """Gap after the `attempts`-th failed attempt: 30 s, 1 min, 2 min, ... at most 1 h."""
    exponent = max(attempts - 1, 0)
    if exponent > 20:
        return MAX_RETRY_GAP
    gap: timedelta = FIRST_RETRY * int(2**exponent)
    return min(gap, MAX_RETRY_GAP)


def private_targets_allowed(settings: Settings) -> bool:
    return settings.profile == "local" and settings.webhook_allow_private_targets


def _address_allowed(address: str, settings: Settings) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_multicast or ip.is_unspecified:
        return False
    if ip.is_global:
        return True
    return private_targets_allowed(settings)


def default_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return sorted({str(info[4][0]) for info in infos})


class UnsafeTarget(Exception):  # noqa: N818 - a refusal, reported as the delivery error
    """The URL resolves to an address webhooks may not reach."""


def check_url(url: str, settings: Settings) -> tuple[str, int]:
    """Validate the URL's shape; returns (host, port). Raises `Problem` (422)."""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise _problem(ProblemCode.VALIDATION_FAILED, "the webhook URL is not valid") from None
    allowed_schemes = {"https"} if settings.profile == "hosted" else {"https", "http"}
    if parts.scheme not in allowed_schemes:
        raise _problem(
            ProblemCode.VALIDATION_FAILED,
            "the webhook URL must use https"
            if settings.profile == "hosted"
            else "the webhook URL must use https (or http)",
        )
    if parts.username or parts.password:
        raise _problem(ProblemCode.VALIDATION_FAILED, "the webhook URL must not contain a login")
    host = (parts.hostname or "").strip().lower()
    if not host:
        raise _problem(ProblemCode.VALIDATION_FAILED, "the webhook URL has no host")
    if parts.fragment:
        raise _problem(ProblemCode.VALIDATION_FAILED, "the webhook URL must not have a fragment")
    effective_port = port or (443 if parts.scheme == "https" else 80)
    literal: str | None = None
    try:
        literal = str(ipaddress.ip_address(host))
    except ValueError:
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            literal = "127.0.0.1"
    if literal is not None and not _address_allowed(literal, settings):
        raise _problem(
            ProblemCode.VALIDATION_FAILED,
            "webhooks cannot be sent to loopback or private addresses"
            + (
                ""
                if settings.profile == "hosted"
                else " unless RUNWAY_WEBHOOK_ALLOW_PRIVATE_TARGETS=true"
            ),
        )
    return host, effective_port


def _event_types(values: Sequence[str]) -> list[str]:
    unknown = sorted(set(values) - EVENT_TYPES)
    if unknown:
        raise _problem(
            ProblemCode.VALIDATION_FAILED,
            "unknown event types: " + ", ".join(unknown[:5]),
        )
    return sorted(set(values))


def _stored_types(endpoint: WebhookEndpoint) -> list[str]:
    try:
        values: Any = json.loads(endpoint.event_types_json or "[]")
    except ValueError:
        return []
    return [str(value) for value in values] if isinstance(values, list) else []


def _creator_scopes(endpoint: WebhookEndpoint) -> frozenset[str] | None:
    """The scopes the endpoint's creator held, or None for rows that predate the column."""
    raw_text = endpoint.creator_scopes
    if raw_text is None:
        return None
    try:
        raw: Any = json.loads(raw_text)
    except ValueError:
        return None
    if not isinstance(raw, list):
        return None
    return frozenset(str(scope) for scope in raw)


def _check_event_visibility(types: Sequence[str], scopes: frozenset[str]) -> None:
    """A webhook may not subscribe to an event family its creator cannot read."""
    invisible = sorted(event_type for event_type in types if not event_visible(scopes, event_type))
    if invisible:
        raise _problem(
            ProblemCode.VALIDATION_FAILED,
            "these event types need scopes you do not hold: " + ", ".join(invisible[:5]),
        )


def _view(endpoint: WebhookEndpoint) -> WebhookView:
    return WebhookView(
        id=endpoint.id,
        url=endpoint.url,
        event_types=_stored_types(endpoint),
        state=endpoint.state,
        description=endpoint.description,
        created_by=endpoint.created_by,
        created_at=endpoint.created_at,
        consecutive_failures=endpoint.consecutive_failures,
        failing_since=endpoint.failing_since,
        last_success_at=endpoint.last_success_at,
        last_failure_at=endpoint.last_failure_at,
        last_error=endpoint.last_error,
        disabled_at=endpoint.disabled_at,
        disabled_reason=endpoint.disabled_reason,
    )


def _delivery_view(row: WebhookDelivery) -> DeliveryView:
    return DeliveryView(
        id=row.id,
        endpoint_id=row.endpoint_id,
        event_id=row.event_id,
        event_type=row.event_type,
        state=row.state,
        attempts=row.attempts,
        next_attempt_at=row.next_attempt_at,
        last_status_code=row.last_status_code,
        last_error=row.last_error,
        created_at=row.created_at,
        delivered_at=row.delivered_at,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _latest_event_id(session: Session, workspace_id: int) -> int:
    value = session.scalar(
        select(func.max(EventRecord.id)).where(EventRecord.workspace_id == workspace_id)
    )
    return int(value or 0)


# ------------------------------------------------------------------ service


class WebhookService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        transport: httpx.BaseTransport | None = None,
        resolver: Resolver = default_resolver,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.database = database
        self.settings = settings
        self.transport = transport
        self.resolver = resolver
        self.clock = clock

    def _cipher(self) -> Any:
        return TokenStore(self.settings.resolved_data_dir / "secrets").cipher()

    # ------------------------------------------------------------ management

    def create(self, principal: Principal, data: WebhookCreate) -> CreatedWebhook:
        require(principal, WORKSPACE_ADMIN)
        url = data.url.strip()
        check_url(url, self.settings)
        types = _event_types(data.event_types)
        # The endpoint may only ask for event families its creator may read;
        # the stored scopes cap its deliveries forever (fan_out filters on it).
        _check_event_visibility(types, principal.scopes)
        secret = SECRET_PREFIX + secrets.token_urlsafe(32)
        encrypted = self._cipher().encrypt(secret.encode("utf-8")).decode("ascii")
        now = self.clock()
        with self.database.session() as session:
            endpoint = WebhookEndpoint(
                workspace_id=principal.workspace_id,
                url=url,
                secret_encrypted=encrypted,
                event_types_json=json.dumps(types),
                state="active",
                description=data.description,
                created_by=f"{principal.kind}:{principal.id}",
                creator_scopes=json.dumps(sorted(principal.scopes)),
                cursor_event_id=_latest_event_id(session, principal.workspace_id),
                consecutive_failures=0,
                created_at=now,
                updated_at=now,
            )
            session.add(endpoint)
            session.flush()
            return CreatedWebhook(**_view(endpoint).model_dump(), secret=secret)

    def _load(self, session: Session, principal: Principal, endpoint_id: int) -> WebhookEndpoint:
        endpoint = session.get(WebhookEndpoint, endpoint_id)
        if endpoint is None or endpoint.workspace_id != principal.workspace_id:
            raise _problem(ProblemCode.NOT_FOUND, "this webhook does not exist")
        return endpoint

    def list_endpoints(self, principal: Principal, params: CursorParams) -> CursorPage[WebhookView]:
        require(principal, WORKSPACE_ADMIN)
        with self.database.session() as session:
            statement = select(WebhookEndpoint).where(
                WebhookEndpoint.workspace_id == principal.workspace_id
            )
            rows, next_cursor = paginate(
                session, statement, WebhookEndpoint.id, params, scope="webhooks"
            )
            return CursorPage[WebhookView](
                items=[_view(row) for row in rows], next_cursor=next_cursor
            )

    def get(self, principal: Principal, endpoint_id: int) -> WebhookView:
        require(principal, WORKSPACE_ADMIN)
        with self.database.session() as session:
            return _view(self._load(session, principal, endpoint_id))

    def update(self, principal: Principal, endpoint_id: int, data: WebhookPatch) -> WebhookView:
        require(principal, WORKSPACE_ADMIN)
        types = _event_types(data.event_types) if data.event_types is not None else None
        now = self.clock()
        with self.database.session() as session:
            endpoint = self._load(session, principal, endpoint_id)
            if types is not None:
                # The ceiling stays the creator's scopes; a row without them
                # (predating the column) is checked against the updater's.
                ceiling = _creator_scopes(endpoint)
                _check_event_visibility(types, ceiling if ceiling is not None else principal.scopes)
                endpoint.event_types_json = json.dumps(types)
            if data.description is not None:
                endpoint.description = data.description
            if data.state == "disabled" and endpoint.state != "disabled":
                self._disable(session, endpoint, now, "disabled by a person")
            elif data.state == "active" and endpoint.state != "active":
                # Re-enabled: start clean from the newest event (no flood of old ones).
                endpoint.state = "active"
                endpoint.consecutive_failures = 0
                endpoint.failing_since = None
                endpoint.disabled_at = None
                endpoint.disabled_reason = None
                endpoint.cursor_event_id = _latest_event_id(session, endpoint.workspace_id)
            endpoint.updated_at = now
            session.flush()
            return _view(endpoint)

    def delete(self, principal: Principal, endpoint_id: int) -> None:
        require(principal, WORKSPACE_ADMIN)
        with self.database.session() as session:
            endpoint = self._load(session, principal, endpoint_id)
            session.execute(
                delete(WebhookDelivery).where(WebhookDelivery.endpoint_id == endpoint.id)
            )
            session.delete(endpoint)

    def deliveries(
        self, principal: Principal, endpoint_id: int, params: CursorParams
    ) -> CursorPage[DeliveryView]:
        require(principal, WORKSPACE_ADMIN)
        with self.database.session() as session:
            endpoint = self._load(session, principal, endpoint_id)
            statement = select(WebhookDelivery).where(
                WebhookDelivery.endpoint_id == endpoint.id,
                WebhookDelivery.workspace_id == principal.workspace_id,
            )
            rows, next_cursor = paginate(
                session, statement, WebhookDelivery.id, params, scope=f"deliveries:{endpoint.id}"
            )
            return CursorPage[DeliveryView](
                items=[_delivery_view(row) for row in rows], next_cursor=next_cursor
            )

    # ------------------------------------------------------------ fan-out

    def fan_out(self) -> int:
        """Turn new events into pending deliveries for every active endpoint."""
        created = 0
        now = self.clock()
        with self.database.session() as session:
            endpoints = session.scalars(
                select(WebhookEndpoint).where(WebhookEndpoint.state == "active")
            ).all()
            for endpoint in endpoints:
                wanted = set(_stored_types(endpoint))
                creator = _creator_scopes(endpoint)
                events = session.scalars(
                    select(EventRecord)
                    .where(
                        EventRecord.workspace_id == endpoint.workspace_id,
                        EventRecord.id > endpoint.cursor_event_id,
                    )
                    .order_by(EventRecord.id)
                    .limit(FAN_OUT_BATCH)
                ).all()
                for event in events:
                    if wanted and event.type not in wanted:
                        continue
                    if creator is not None and not event_visible(creator, event.type):
                        # The creator could never read this family; the event is
                        # skipped for this endpoint but the cursor still advances.
                        continue
                    session.add(
                        WebhookDelivery(
                            workspace_id=endpoint.workspace_id,
                            endpoint_id=endpoint.id,
                            event_id=event.id,
                            event_type=event.type,
                            state="pending",
                            attempts=0,
                            next_attempt_at=now,
                            created_at=now,
                        )
                    )
                    created += 1
                if events:
                    endpoint.cursor_event_id = events[-1].id
            session.flush()
        return created

    def due_deliveries(self, now: datetime) -> list[tuple[int, int, int]]:
        """(delivery id, attempts, workspace id) of pending deliveries due now."""
        with self.database.session() as session:
            rows = session.execute(
                select(WebhookDelivery.id, WebhookDelivery.attempts, WebhookDelivery.workspace_id)
                .where(WebhookDelivery.state == "pending", WebhookDelivery.next_attempt_at <= now)
                .order_by(WebhookDelivery.next_attempt_at, WebhookDelivery.id)
                .limit(DUE_BATCH)
            ).all()
        return [(int(row[0]), int(row[1]), int(row[2])) for row in rows]

    def next_attempt_at(self) -> datetime | None:
        with self.database.session() as session:
            value = session.scalar(
                select(func.min(WebhookDelivery.next_attempt_at)).where(
                    WebhookDelivery.state == "pending"
                )
            )
        return _aware(value) if value is not None else None

    # ------------------------------------------------------------ delivery

    def _pinned_target(self, url: str) -> tuple[httpx.URL, str, dict[str, str]]:
        """Resolve `url`'s host once and pin the request to the validated answer.

        Every resolved address must pass the SSRF check; the request then goes
        only to the first validated address, so a second DNS answer cannot
        rebind the delivery to a private target (spec 06 section 8). Returns
        the IP-literal request URL, the `Host` value to send and the httpcore
        `sni_hostname` extension that keeps TLS verified for the original host.
        """
        check_url(url, self.settings)
        original = httpx.URL(url)
        host = original.host
        port = original.port or (443 if original.scheme == "https" else 80)
        try:
            addresses = list(self.resolver(host, port))
        except OSError as exc:
            raise UnsafeTarget("the host does not resolve") from exc
        if not addresses:
            raise UnsafeTarget("the host does not resolve")
        for address in addresses:
            if not _address_allowed(address, self.settings):
                raise UnsafeTarget("the host resolves to a loopback or private address")
        pinned = original.copy_with(host=addresses[0].split("%", 1)[0], port=port)
        # An IPv6 literal is bracketed in Host; the port is sent only when it
        # is not the scheme's default.
        display_host = f"[{host}]" if ":" in host else host
        default_port = 443 if original.scheme == "https" else 80
        host_header = display_host if port == default_port else f"{display_host}:{port}"
        extensions = {"sni_hostname": host} if original.scheme == "https" else {}
        return pinned, host_header, extensions

    def body_for(self, event: EventRecord, delivery_id: int) -> bytes:
        data = Event.of(event).as_dict()
        envelope = {
            "delivery_id": delivery_id,
            "event_id": event.id,
            "type": event.type,
            "workspace_id": event.workspace_id,
            "created_at": data.get("created_at"),
            "event": data,
        }
        return json.dumps(envelope, separators=(",", ":"), sort_keys=True, default=str).encode(
            "utf-8"
        )

    def deliver(self, delivery_id: int) -> str:
        """One attempt for one delivery; returns the delivery's new state.

        Everything that can fail — loading the rows, decrypting the secret,
        building the body, resolving DNS and the request itself — sits inside
        one try, so any failure is recorded as a failed attempt with its
        backoff, never left as a wedged `pending` row.
        """
        endpoint_id: int | None = None
        status_code: int | None = None
        error: str | None = None
        setup_error: Exception | None = None
        try:
            with self.database.session() as session:
                delivery = session.get(WebhookDelivery, delivery_id)
                if delivery is None or delivery.state != "pending":
                    return delivery.state if delivery is not None else "missing"
                endpoint = session.get(WebhookEndpoint, delivery.endpoint_id)
                if endpoint is None or endpoint.state != "active":
                    delivery.state = "cancelled"
                    return delivery.state
                event = session.get(EventRecord, delivery.event_id)
                if event is None:
                    # Purged after 7 days; nothing left to send.
                    delivery.state = "expired"
                    return delivery.state
                endpoint_id = endpoint.id
                url = endpoint.url
                event_type = event.type
                body = self.body_for(event, delivery.id)
                secret = self._cipher().decrypt(endpoint.secret_encrypted.encode("ascii")).decode()
            pinned, host_header, extensions = self._pinned_target(url)
            now_ts = int(self.clock().timestamp())
            headers = {
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
                "Host": host_header,
                SIGNATURE_HEADER: sign(secret, now_ts, body),
                "Runway-Event": event_type,
                "Runway-Delivery": str(delivery_id),
            }
            with httpx.Client(
                transport=self.transport,
                timeout=self.settings.webhook_timeout_seconds,
                follow_redirects=False,
                trust_env=False,
            ) as client:
                response = client.post(pinned, content=body, headers=headers, extensions=extensions)
            status_code = response.status_code
            if not 200 <= status_code < 300:
                error = f"HTTP {status_code}"
        except UnsafeTarget as exc:
            error = f"refused: {exc}"
        except Problem as exc:
            error = f"refused: {exc.detail}"[:300]
        except httpx.HTTPError as exc:
            error = type(exc).__name__
        except Exception as exc:
            # A setup failure (an undecryptable secret, an unbuildable body, a
            # database error) is a failed attempt like any other.
            setup_error = exc
            error = f"setup: {type(exc).__name__}"
            logger.exception("webhook delivery %s failed before the request", delivery_id)
        if endpoint_id is None:
            # The endpoint row was never reached, so there is nowhere to
            # record; let the job fail and retry instead of wedging silently.
            assert setup_error is not None  # noqa: S101 - unreachable on the early returns
            raise setup_error
        return self._record(delivery_id, endpoint_id, status_code, error)

    def _record(
        self, delivery_id: int, endpoint_id: int, status_code: int | None, error: str | None
    ) -> str:
        now = self.clock()
        with self.database.session() as session:
            delivery = session.get(WebhookDelivery, delivery_id)
            endpoint = session.get(WebhookEndpoint, endpoint_id)
            if delivery is None or endpoint is None:
                return "missing"
            delivery.attempts += 1
            delivery.last_status_code = status_code
            if error is None:
                delivery.state = "delivered"
                delivery.delivered_at = now
                delivery.last_error = None
                endpoint.consecutive_failures = 0
                endpoint.failing_since = None
                endpoint.last_success_at = now
                endpoint.last_error = None
                return delivery.state
            delivery.last_error = error[:300]
            endpoint.consecutive_failures += 1
            endpoint.last_failure_at = now
            endpoint.last_error = error[:300]
            if endpoint.failing_since is None:
                endpoint.failing_since = now
            if now - _aware(delivery.created_at) >= RETRY_WINDOW:
                delivery.state = "failed"
            else:
                delivery.next_attempt_at = now + retry_delay(delivery.attempts)
            if (
                endpoint.state == "active"
                and endpoint.consecutive_failures >= DISABLE_AFTER_FAILURES
                and now - _aware(endpoint.failing_since) >= DISABLE_AFTER
            ):
                self._disable(
                    session, endpoint, now, "failing for 24 hours; fix the receiver, then enable"
                )
            session.flush()
            return delivery.state

    @staticmethod
    def _disable(session: Session, endpoint: WebhookEndpoint, now: datetime, reason: str) -> None:
        endpoint.state = "disabled"
        endpoint.disabled_at = now
        endpoint.disabled_reason = reason
        session.execute(
            update(WebhookDelivery)
            .where(WebhookDelivery.endpoint_id == endpoint.id, WebhookDelivery.state == "pending")
            .values(state="cancelled")
        )
        logger.warning("webhook endpoint %s disabled: %s", endpoint.id, reason)


# ------------------------------------------------------------------ worker


class WebhookDispatcher:
    """Worker producer: fan events out, then enqueue one job per due delivery attempt."""

    def __init__(self, service: WebhookService, queue: JobQueue) -> None:
        self.service = service
        self.queue = queue

    def produce(self, now: datetime) -> None:
        self.service.fan_out()
        for delivery_id, attempts, workspace_id in self.service.due_deliveries(now):
            self.queue.enqueue(
                DELIVER_WEBHOOK_JOB,
                idempotency_key=f"{DELIVER_WEBHOOK_JOB}:{delivery_id}:{attempts}",
                run_at=now,
                workspace_id=workspace_id,
                payload={"delivery_id": delivery_id},
                max_attempts=1,
            )

    def next_due(self, now: datetime) -> datetime:
        upcoming = self.service.next_attempt_at()
        poll = now + FAN_OUT_INTERVAL
        return min(upcoming, poll) if upcoming is not None else poll


def run_deliver_webhook_job(service: WebhookService, job: Job) -> None:
    delivery_id = job.payload.get("delivery_id")
    if isinstance(delivery_id, int):
        service.deliver(delivery_id)
