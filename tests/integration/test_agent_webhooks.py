"""Outbound webhooks and the key-filtered event stream (spec 06 section 8)."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from runway.agent.webhooks import (
    DELIVER_WEBHOOK_JOB,
    SIGNATURE_HEADER,
    WebhookCreate,
    WebhookDispatcher,
    WebhookPatch,
    WebhookService,
    check_url,
    retry_delay,
    run_deliver_webhook_job,
    verify_signature,
)
from runway.api.app import create_app
from runway.auth.api_keys import ApiKeyService
from runway.auth.principal import Principal, local_owner
from runway.auth.problems import Problem
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import PublishJob, WebhookDelivery, WebhookEndpoint
from runway.events import types
from runway.events.log import record_event
from runway.events.stream import event_stream
from runway.events.visibility import visibility_for
from runway.jobs.queue import JobQueue

BASE_URL = "http://127.0.0.1:8000"
RECEIVER = "https://hooks.example.test/runway"


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class Receiver:
    """An httpx MockTransport that records requests and answers with `status`."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status, text="secret receiver detail")

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _public(_host: str, _port: int) -> list[str]:
    return ["93.184.216.34"]


def _private(_host: str, _port: int) -> list[str]:
    return ["10.0.0.5"]


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def receiver() -> Receiver:
    return Receiver()


@pytest.fixture()
def service(
    database: Database, settings: Settings, clock: Clock, receiver: Receiver
) -> WebhookService:
    return WebhookService(
        database, settings, transport=receiver.transport(), resolver=_public, clock=clock
    )


@pytest.fixture()
def client(settings: Settings, database: Database) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url=BASE_URL) as test_client:
        yield test_client


def _event(database: Database, event_type: str = types.TARGET_AWAITING_RELEASE) -> int:
    with database.session() as session:
        return record_event(session, 1, event_type, "target", 7, {"target_id": 7}).id


def _delivery(database: Database) -> WebhookDelivery:
    with database.session() as session:
        row = session.scalars(select(WebhookDelivery)).one()
        session.expunge(row)
        return row


# ------------------------------------------------------------------ routes


def test_endpoints_need_workspace_admin_and_show_the_secret_once(
    client: TestClient, database: Database, settings: Settings
) -> None:
    agent = ApiKeyService(database, settings).create(local_owner(), name="Muse").secret
    body = {"url": RECEIVER, "event_types": ["target.rejected", "target.awaiting_release"]}
    refused = client.post(
        "/api/v1/webhooks", json=body, headers={"Authorization": f"Bearer {agent}"}
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "scope_missing"

    created = client.post("/api/v1/webhooks", json=body)
    assert created.status_code == 201, created.text
    data = created.json()
    secret = data["secret"]
    assert secret.startswith("whsec_") and len(secret) > 40
    assert data["event_types"] == ["target.awaiting_release", "target.rejected"]
    assert data["state"] == "active"
    shown = client.get(f"/api/v1/webhooks/{data['id']}").json()
    assert "secret" not in shown
    listed = client.get("/api/v1/webhooks").json()
    assert [item["id"] for item in listed["items"]] == [data["id"]]
    with database.session() as session:
        stored = session.get(WebhookEndpoint, data["id"])
        assert stored is not None and secret not in stored.secret_encrypted

    unknown = client.post("/api/v1/webhooks", json={"url": RECEIVER, "event_types": ["nope.x"]})
    assert unknown.status_code == 422
    loopback = client.post("/api/v1/webhooks", json={"url": "http://127.0.0.1:9/hook"})
    assert loopback.status_code == 422
    assert loopback.json()["code"] == "validation_failed"

    disabled = client.patch(f"/api/v1/webhooks/{data['id']}", json={"state": "disabled"})
    assert disabled.status_code == 200 and disabled.json()["state"] == "disabled"
    assert client.delete(f"/api/v1/webhooks/{data['id']}").status_code == 204
    assert client.get(f"/api/v1/webhooks/{data['id']}").status_code == 404


def test_ssrf_guard_at_creation_by_profile(settings: Settings) -> None:
    hosted = settings.model_copy(
        update={"profile": "hosted", "webhook_allow_private_targets": True}
    )
    allowed_local = settings.model_copy(update={"webhook_allow_private_targets": True})
    for url in (
        "http://127.0.0.1/hook",
        "https://localhost/hook",
        "https://10.1.2.3/hook",
        "https://[::1]/hook",
        "https://169.254.169.254/latest",
        "https://[::ffff:192.168.0.1]/hook",
    ):
        with pytest.raises(Problem):
            check_url(url, settings)
        # Hosted never allows it, even with the setting on.
        with pytest.raises(Problem):
            check_url(url, hosted)
    # Local, explicitly allowed by the owner.
    assert check_url("http://127.0.0.1:8123/hook", allowed_local) == ("127.0.0.1", 8123)
    # Hosted needs https; neither profile accepts a login in the URL.
    with pytest.raises(Problem):
        check_url("http://hooks.example.test/x", hosted)
    with pytest.raises(Problem):
        check_url("https://user:pw@hooks.example.test/x", settings)
    assert check_url(RECEIVER, hosted) == ("hooks.example.test", 443)


# ---------------------------------------------------------------- delivery


def test_events_are_delivered_signed_filtered_and_never_replayed(
    database: Database, service: WebhookService, receiver: Receiver, clock: Clock
) -> None:
    _event(database)  # before the endpoint exists: never delivered
    created = service.create(
        local_owner(),
        WebhookCreate(url=RECEIVER, event_types=[types.TARGET_AWAITING_RELEASE]),
    )
    wanted = _event(database, types.TARGET_AWAITING_RELEASE)
    _event(database, types.COMMENT_NEW)  # filtered out
    assert service.fan_out() == 1
    assert service.fan_out() == 0
    delivery = _delivery(database)
    assert delivery.event_id == wanted

    assert service.deliver(delivery.id) == "delivered"
    request = receiver.requests[0]
    body = request.content
    header = request.headers[SIGNATURE_HEADER]
    timestamp = int(clock.now.timestamp())
    expected = hmac.new(
        created.secret.encode(), f"{timestamp}.".encode() + body, hashlib.sha256
    ).hexdigest()
    assert header == f"t={timestamp},s={expected}"
    assert verify_signature(created.secret, header, body)
    assert not verify_signature(created.secret, header, body + b" ")
    assert not verify_signature("whsec_other", header, body)
    payload = json.loads(body)
    assert payload["type"] == types.TARGET_AWAITING_RELEASE
    assert payload["event_id"] == wanted
    assert payload["event"]["payload"] == {"target_id": 7}
    assert request.headers["Runway-Event"] == types.TARGET_AWAITING_RELEASE
    # Delivered once: a second attempt is a no-op.
    assert service.deliver(delivery.id) == "delivered"
    assert len(receiver.requests) == 1


def test_failures_back_off_for_24_hours_then_the_endpoint_is_disabled(
    database: Database, service: WebhookService, receiver: Receiver, clock: Clock
) -> None:
    receiver.status = 500
    endpoint = service.create(local_owner(), WebhookCreate(url=RECEIVER))
    _event(database)
    service.fan_out()
    delivery_id = _delivery(database).id

    assert service.deliver(delivery_id) == "pending"
    row = _delivery(database)
    assert row.attempts == 1 and row.last_status_code == 500
    assert row.last_error == "HTTP 500"  # Runway's summary, never the receiver's body
    assert row.next_attempt_at.replace(tzinfo=UTC) == clock.now + timedelta(seconds=30)
    assert retry_delay(2) == timedelta(minutes=1)
    assert retry_delay(30) == timedelta(hours=1)

    attempts = 1
    while clock.now - datetime(2026, 11, 23, 12, 0, tzinfo=UTC) < timedelta(hours=24):
        clock.now = _delivery(database).next_attempt_at.replace(tzinfo=UTC)
        state = service.deliver(delivery_id)
        attempts += 1
        if state != "pending":
            break
    assert _delivery(database).state == "failed"
    assert 10 <= attempts <= 40
    view = service.get(local_owner(), endpoint.id)
    assert view.state == "disabled"
    assert view.disabled_reason and "24 hours" in view.disabled_reason
    assert view.consecutive_failures == attempts

    # A disabled endpoint gets no new deliveries; re-enabling starts from the newest event.
    _event(database)
    assert service.fan_out() == 0
    enabled = service.update(local_owner(), endpoint.id, WebhookPatch(state="active"))
    assert enabled.state == "active" and enabled.consecutive_failures == 0
    assert service.fan_out() == 0


def test_a_success_resets_the_failure_streak(
    database: Database, service: WebhookService, receiver: Receiver
) -> None:
    receiver.status = 503
    endpoint = service.create(local_owner(), WebhookCreate(url=RECEIVER))
    _event(database)
    service.fan_out()
    delivery_id = _delivery(database).id
    service.deliver(delivery_id)
    receiver.status = 204
    with database.session() as session:
        row = session.get(WebhookDelivery, delivery_id)
        assert row is not None
        row.next_attempt_at = row.created_at
    assert service.deliver(delivery_id) == "delivered"
    view = service.get(local_owner(), endpoint.id)
    assert view.consecutive_failures == 0 and view.failing_since is None
    assert view.last_success_at is not None


def test_delivery_refuses_hosts_that_resolve_to_private_addresses(
    database: Database, settings: Settings, clock: Clock, receiver: Receiver
) -> None:
    service = WebhookService(
        database, settings, transport=receiver.transport(), resolver=_private, clock=clock
    )
    service.create(local_owner(), WebhookCreate(url=RECEIVER))
    _event(database)
    service.fan_out()
    delivery_id = _delivery(database).id
    assert service.deliver(delivery_id) == "pending"
    assert receiver.requests == []
    row = _delivery(database)
    assert row.last_error is not None and row.last_error.startswith("refused:")
    # Allowed in local only when the owner turned it on.
    allowed = settings.model_copy(update={"webhook_allow_private_targets": True})
    service = WebhookService(
        database, allowed, transport=receiver.transport(), resolver=_private, clock=clock
    )
    with database.session() as session:
        stored = session.get(WebhookDelivery, delivery_id)
        assert stored is not None
        stored.next_attempt_at = stored.created_at
    assert service.deliver(delivery_id) == "delivered"
    assert len(receiver.requests) == 1


def test_redirects_are_not_followed(database: Database, settings: Settings, clock: Clock) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})

    service = WebhookService(
        database, settings, transport=httpx.MockTransport(handler), resolver=_public, clock=clock
    )
    service.create(local_owner(), WebhookCreate(url=RECEIVER))
    _event(database)
    service.fan_out()
    assert service.deliver(_delivery(database).id) == "pending"
    # The request goes to the resolved IP, not the hostname (DNS is resolved
    # once and pinned), and the redirect is still not followed.
    assert seen == ["https://93.184.216.34/runway"]


def test_webhook_administration_is_for_people_only(
    client: TestClient, database: Database, settings: Settings
) -> None:
    """Outbount destinations and signing secrets are a person's to manage."""
    agent = ApiKeyService(database, settings).create(local_owner(), name="Muse").secret
    bearer = {"Authorization": f"Bearer {agent}"}
    created = client.post("/api/v1/webhooks", json={"url": RECEIVER})
    assert created.status_code == 201, created.text
    endpoint_id = created.json()["id"]
    for method, url, body in (
        ("get", "/api/v1/webhooks", None),
        ("post", "/api/v1/webhooks", {"url": RECEIVER}),
        ("get", f"/api/v1/webhooks/{endpoint_id}", None),
        ("patch", f"/api/v1/webhooks/{endpoint_id}", {"description": "x"}),
        ("delete", f"/api/v1/webhooks/{endpoint_id}", None),
        ("get", f"/api/v1/webhooks/{endpoint_id}/deliveries", None),
    ):
        response = client.request(method, url, json=body, headers=bearer)
        assert response.status_code == 403, (method, url, response.text)
        assert response.json()["code"] == "scope_missing"
    # The owner still manages them.
    assert client.get("/api/v1/webhooks").status_code == 200
    assert client.get(f"/api/v1/webhooks/{endpoint_id}/deliveries").status_code == 200


def test_delivery_pins_the_first_validated_address_and_keeps_the_host(
    database: Database, settings: Settings, clock: Clock
) -> None:
    """DNS is resolved once, every answer is checked, and the request goes only
    to the first address — a later answer cannot rebind the delivery."""
    settings = settings.model_copy(update={"webhook_allow_private_targets": True})
    resolved: list[tuple[str, int]] = []

    def resolver(host: str, port: int) -> list[str]:
        resolved.append((host, port))
        return ["93.184.216.34", "127.0.0.1"]

    receiver = Receiver()
    service = WebhookService(
        database, settings, transport=receiver.transport(), resolver=resolver, clock=clock
    )
    service.create(local_owner(), WebhookCreate(url=RECEIVER))
    _event(database)
    service.fan_out()
    assert service.deliver(_delivery(database).id) == "delivered"
    # Resolved exactly once, for the original host and port.
    assert resolved == [("hooks.example.test", 443)]
    request = receiver.requests[0]
    assert str(request.url) == "https://93.184.216.34/runway"
    assert request.headers["host"] == "hooks.example.test"
    # TLS still proves the original hostname (SNI + certificate check).
    assert request.extensions.get("sni_hostname") == "hooks.example.test"


def test_a_non_default_port_is_kept_in_the_host_header_and_pinned_url(
    database: Database, settings: Settings, clock: Clock
) -> None:
    settings = settings.model_copy(update={"webhook_allow_private_targets": True})
    receiver = Receiver()
    service = WebhookService(
        database, settings, transport=receiver.transport(), resolver=_public, clock=clock
    )
    service.create(local_owner(), WebhookCreate(url="http://hooks.example.test:8123/hook"))
    _event(database)
    service.fan_out()
    assert service.deliver(_delivery(database).id) == "delivered"
    request = receiver.requests[0]
    assert str(request.url) == "http://93.184.216.34:8123/hook"
    assert request.headers["host"] == "hooks.example.test:8123"


def test_an_endpoint_never_delivers_events_its_creator_could_not_read(
    database: Database, service: WebhookService
) -> None:
    """The endpoint's stored creator scopes cap fan-out and updates forever."""
    limited = Principal(
        kind="user",
        id="admin-no-inbox",
        workspace_id=1,
        role=None,
        scopes=frozenset({"workspace:admin", "posts:read"}),
        display="limited admin",
    )
    # Creating a subscription to an invisible family is refused outright.
    with pytest.raises(Problem) as raised:
        service.create(
            limited, WebhookCreate(url=RECEIVER, event_types=[types.COMMENT_NEW])
        )
    assert raised.value.code == "validation_failed"
    endpoint = service.create(limited, WebhookCreate(url=RECEIVER))
    with database.session() as session:
        stored = session.get(WebhookEndpoint, endpoint.id)
        assert stored is not None
        assert json.loads(stored.creator_scopes) == sorted(limited.scopes)
    invisible = _event(database, types.COMMENT_NEW)
    wanted = _event(database, types.TARGET_AWAITING_RELEASE)
    assert service.fan_out() == 1
    delivery = _delivery(database)
    assert delivery.event_id == wanted != invisible
    assert delivery.event_type == types.TARGET_AWAITING_RELEASE
    # The stored ceiling binds updates too, whoever makes them.
    with pytest.raises(Problem) as raised:
        service.update(
            local_owner(), endpoint.id, WebhookPatch(event_types=[types.COMMENT_NEW])
        )
    assert raised.value.code == "validation_failed"


def test_an_undecryptable_secret_is_recorded_as_a_failed_attempt(
    database: Database, service: WebhookService, receiver: Receiver
) -> None:
    """Setup failures (here: a secret that no longer decrypts) advance the
    retry state instead of wedging the delivery as pending forever."""
    endpoint = service.create(local_owner(), WebhookCreate(url=RECEIVER))
    _event(database)
    service.fan_out()
    delivery_id = _delivery(database).id
    with database.session() as session:
        stored = session.get(WebhookEndpoint, endpoint.id)
        assert stored is not None
        stored.secret_encrypted = "not-a-fernet-token"
    assert service.deliver(delivery_id) == "pending"
    assert receiver.requests == []
    row = _delivery(database)
    assert row.attempts == 1
    assert row.last_error is not None and "setup" in row.last_error
    assert row.next_attempt_at > row.created_at
    view = service.get(local_owner(), endpoint.id)
    assert view.consecutive_failures == 1
    assert view.last_failure_at is not None
    assert view.last_error and "setup" in view.last_error


def test_the_worker_producer_enqueues_one_job_per_due_attempt(
    database: Database, service: WebhookService, receiver: Receiver, clock: Clock
) -> None:
    queue = JobQueue(database, clock=clock)
    dispatcher = WebhookDispatcher(service, queue)
    service.create(local_owner(), WebhookCreate(url=RECEIVER))
    _event(database)
    dispatcher.produce(clock.now)
    dispatcher.produce(clock.now)  # same attempt: same idempotency key, one job
    with database.session() as session:
        jobs = session.scalars(
            select(PublishJob).where(PublishJob.kind == DELIVER_WEBHOOK_JOB)
        ).all()
        assert len(jobs) == 1
        job_id = jobs[0].id
    job = queue.get(job_id)
    assert job is not None
    run_deliver_webhook_job(service, job)
    assert len(receiver.requests) == 1
    assert dispatcher.next_due(clock.now) == clock.now + timedelta(seconds=10)


# ------------------------------------------------------------ event stream


def _stream(database: Database, after: int, visible: Any, frames_wanted: int) -> list[str]:
    frames: list[str] = []

    class Wake:
        async def wait(self, timeout: float) -> None:
            await anyio.lowlevel.checkpoint()

    async def disconnected() -> bool:
        return len(frames) >= frames_wanted

    async def run() -> None:
        async for frame in event_stream(
            database, 1, after, wakeup=Wake(), is_disconnected=disconnected, visible=visible
        ):
            frames.append(frame)

    anyio.run(run)
    return frames


def test_the_event_stream_is_filtered_to_the_keys_readable_scopes(
    database: Database, settings: Settings
) -> None:
    keys = ApiKeyService(database, settings)
    posts_only = keys.authenticate(
        keys.create(local_owner(), name="Posts", scopes=["posts:read"]).secret
    )
    inbox_only = keys.authenticate(
        keys.create(local_owner(), name="Inbox", scopes=["inbox:read"]).secret
    )
    # Resume after a first event, as a client with Last-Event-ID would.
    start = _event(database, types.SYNC_STARTED)
    _event(database, types.COMMENT_NEW)
    _event(database, types.TARGET_REJECTED)
    _event(database, types.TARGET_AWAITING_RELEASE)

    def kinds(frames: list[str]) -> list[str]:
        return [
            line.removeprefix("event: ")
            for frame in frames
            for line in frame.splitlines()
            if line.startswith("event: ")
        ]

    assert kinds(_stream(database, start, visibility_for(posts_only), 3)) == [
        types.TARGET_REJECTED,
        types.TARGET_AWAITING_RELEASE,
    ]
    assert kinds(_stream(database, start, visibility_for(inbox_only), 2)) == [types.COMMENT_NEW]
    assert kinds(_stream(database, start, visibility_for(local_owner()), 4)) == [
        types.COMMENT_NEW,
        types.TARGET_REJECTED,
        types.TARGET_AWAITING_RELEASE,
    ]
