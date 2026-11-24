"""Webhook inbox acceptance tests (spec 04 section 2 and section 7).

Every POST route enforces, in order: the 1 MB body cap, a per-source rate
limit, signature verification on the raw bytes, persistence of the raw event,
and a `process_webhook` job in the same transaction. The worker then treats
the stored body as a hint: it resolves the account and enqueues a targeted
fetch, never trusting platform data taken from the body.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from runway.api.app import create_app
from runway.config import Settings
from runway.db import initialize_database, models
from runway.db.base import Database
from runway.db.models import ConnectedAccount, EventRecord, PublishJob, utcnow
from runway.jobs.queue import Job, JobQueue
from runway.jobs.runtime import build_worker
from runway.jobs.worker import Worker
from runway.realtime.processing import PROCESS_WEBHOOK_JOB, run_process_webhook_job

IG_APP_SECRET = "test-instagram-app-secret"
IG_VERIFY_TOKEN = "test-instagram-verify-token"
TT_CLIENT_SECRET = "test-tiktok-client-secret"
WEBSUB_SECRET = "test-websub-hub-secret"
CALLBACK_TOKEN = "ab" * 32  # what a real 32-byte hex callback token looks like
YOUTUBE_TOPIC = "https://www.youtube.com/xml/feeds/videos.xml?channel_id=UC-test"
MAX_BODY_BYTES = 1_048_576

ATOM_FEED = (
    b'<?xml version="1.0" encoding="UTF-8"?>\n'
    b'<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" '
    b'xmlns="http://www.w3.org/2005/Atom">\n'
    b"  <entry>\n"
    b"    <id>yt:video:VIDEO-1</id>\n"
    b"    <yt:videoId>VIDEO-1</yt:videoId>\n"
    b"    <yt:channelId>UC-test</yt:channelId>\n"
    b"    <title>upload</title>\n"
    b"  </entry>\n"
    b"</feed>\n"
)


@pytest.fixture()
def hook_app(tmp_path: Path) -> Iterator[tuple[FastAPI, Database, Settings]]:
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        instagram_app_secret=IG_APP_SECRET,
        instagram_webhook_verify_token=IG_VERIFY_TOKEN,
        tiktok_client_secret=TT_CLIENT_SECRET,
    )
    database = initialize_database(settings)
    application = create_app(settings)
    try:
        yield application, database, settings
    finally:
        database.engine.dispose()
        application.state.database.engine.dispose()


@pytest.fixture()
def client(hook_app: tuple[FastAPI, Database, Settings]) -> TestClient:
    return TestClient(hook_app[0], base_url="http://127.0.0.1:8000")


# --------------------------------------------------------------- test helpers


def _account(database: Database, platform: str, external_id: str, status: str = "connected") -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform=platform,
            external_account_id=external_id,
            status=status,
            granted_scopes_json="[]",
        )
        session.add(account)
        session.flush()
        return int(account.id)


def _subscription(
    database: Database,
    account_id: int,
    *,
    secret: str | None = WEBSUB_SECRET,
    state: str = "pending",
    callback_token: str | None = CALLBACK_TOKEN,
) -> int:
    with database.session() as session:
        subscription = models.PlatformSubscription(
            account_id=account_id,
            provider="youtube",
            topic=YOUTUBE_TOPIC,
            hub_secret=secret,
            callback_token=callback_token,
            state=state,
        )
        session.add(subscription)
        session.flush()
        return int(subscription.id)


def _events(database: Database) -> list[models.WebhookEvent]:
    with database.session() as session:
        rows = session.scalars(select(models.WebhookEvent).order_by(models.WebhookEvent.id)).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def _jobs(database: Database, kind: str | None = None) -> list[PublishJob]:
    with database.session() as session:
        statement = select(PublishJob).order_by(PublishJob.id)
        if kind is not None:
            statement = statement.where(PublishJob.kind == kind)
        rows = session.scalars(statement).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def _tiktok_signature(
    body: bytes, *, timestamp: int | None = None, secret: str = TT_CLIENT_SECRET
) -> str:
    moment = int(time.time()) if timestamp is None else timestamp
    signature = hmac.new(secret.encode(), f"{moment}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={moment},s={signature}"


def _run_one_process_webhook(database: Database, settings: Settings) -> Job:
    """Claim and run exactly the pending `process_webhook` job (never other kinds)."""
    queue = JobQueue(database)
    job = queue.claim("w-test", {PROCESS_WEBHOOK_JOB})
    assert job is not None, "expected a pending process_webhook job"
    run_process_webhook_job(database, settings, queue, job)
    queue.complete(job.id, "w-test")
    return job


# ------------------------------------------------------------- the scenarios


def test_a_forged_tiktok_webhook_is_stored_unverified_and_triggers_nothing(
    client: TestClient, hook_app: tuple[FastAPI, Database, Settings]
) -> None:
    database = hook_app[1]
    body = b'{"event":"post.publish.complete","user_openid":"openid-1"}'
    forged = f"t={int(time.time())},s={'0' * 64}"

    response = client.post("/hooks/tiktok", content=body, headers={"TikTok-Signature": forged})

    assert response.status_code == 403
    events = _events(database)
    assert len(events) == 1
    event = events[0]
    assert event.provider == "tiktok"
    assert event.signature_ok is False
    assert event.dedupe_key is None  # a forged copy can never block a real one
    assert event.body == body
    assert event.processed_at is None
    assert _jobs(database) == []


def test_ten_replays_of_one_valid_tiktok_webhook_cause_exactly_one_fetch(
    client: TestClient, hook_app: tuple[FastAPI, Database, Settings]
) -> None:
    database = hook_app[1]
    account_id = _account(database, "tiktok", "openid-1")
    body = json.dumps({"event": "user.profile.updated", "user_openid": "openid-1"}).encode()

    for _ in range(10):
        response = client.post(
            "/hooks/tiktok",
            content=body,
            headers={"TikTok-Signature": _tiktok_signature(body)},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True}

    events = _events(database)
    assert len(events) == 1
    assert events[0].signature_ok is True
    assert events[0].dedupe_key == f"tiktok:{hashlib.sha256(body).hexdigest()}"
    process_jobs = _jobs(database, PROCESS_WEBHOOK_JOB)
    assert len(process_jobs) == 1
    assert json.loads(process_jobs[0].payload_json) == {"event_id": events[0].id}
    assert process_jobs[0].idempotency_key == f"webhook:{events[0].id}"

    _run_one_process_webhook(database, hook_app[2])

    sync_jobs = _jobs(database, "sync_account")
    assert len(sync_jobs) == 1
    payload = json.loads(sync_jobs[0].payload_json)
    assert payload["account_id"] == account_id
    assert payload["trigger"] == "webhook"
    processed = _events(database)[0]
    assert processed.processed_at is not None
    assert processed.account_id == account_id
    assert processed.workspace_id == 1
    assert processed.error is None


def test_a_tiktok_timestamp_older_than_five_minutes_fails_verification(
    client: TestClient, hook_app: tuple[FastAPI, Database, Settings]
) -> None:
    database = hook_app[1]
    body = b'{"event":"user.profile.updated","user_openid":"openid-1"}'
    stale = _tiktok_signature(body, timestamp=int(time.time()) - 301)

    response = client.post("/hooks/tiktok", content=body, headers={"TikTok-Signature": stale})

    assert response.status_code == 403
    events = _events(database)
    assert len(events) == 1
    assert events[0].signature_ok is False
    assert events[0].dedupe_key is None
    assert _jobs(database) == []


def test_youtube_websub_verification_signed_delivery_and_dedupe(
    client: TestClient, hook_app: tuple[FastAPI, Database, Settings]
) -> None:
    database = hook_app[1]
    account_id = _account(database, "youtube", "UC-test")
    subscription_id = _subscription(database, account_id)
    callback = f"/hooks/youtube/websub/{subscription_id}/{CALLBACK_TOKEN}"

    verified = client.get(
        callback,
        params={
            "hub.mode": "subscribe",
            "hub.topic": YOUTUBE_TOPIC,
            "hub.challenge": "challenge-abc",
            "hub.lease_seconds": "432000",
        },
    )
    assert verified.status_code == 200
    assert verified.headers["content-type"].startswith("text/plain")
    assert verified.text == "challenge-abc"
    with database.session() as session:
        subscription = session.get(models.PlatformSubscription, subscription_id)
        assert subscription is not None
        assert subscription.state == "active"
        assert subscription.lease_seconds == 432000
        assert subscription.subscribed_at is not None
        assert subscription.expires_at is not None
        delta = subscription.expires_at - subscription.subscribed_at
        assert delta.total_seconds() == 432000

    wrong_topic = client.get(
        callback,
        params={
            "hub.mode": "subscribe",
            "hub.topic": YOUTUBE_TOPIC + "-forged",
            "hub.challenge": "nope",
            "hub.lease_seconds": "432000",
        },
    )
    assert wrong_topic.status_code == 404

    sha1 = "sha1=" + hmac.new(WEBSUB_SECRET.encode(), ATOM_FEED, hashlib.sha1).hexdigest()
    delivered = client.post(
        callback,
        content=ATOM_FEED,
        headers={"X-Hub-Signature": sha1},
    )
    assert delivered.status_code == 200
    assert delivered.json() == {"ok": True}

    other_feed = ATOM_FEED.replace(b"VIDEO-1", b"VIDEO-2")
    sha256 = "sha256=" + hmac.new(WEBSUB_SECRET.encode(), other_feed, hashlib.sha256).hexdigest()
    delivered_again = client.post(
        callback,
        content=other_feed,
        headers={"X-Hub-Signature": sha256},
    )
    assert delivered_again.status_code == 200

    unsigned = client.post(
        callback,
        content=ATOM_FEED.replace(b"VIDEO-1", b"VIDEO-3"),
    )
    assert unsigned.status_code == 403

    events = _events(database)
    assert [event.signature_ok for event in events] == [True, True, False]
    assert len(_jobs(database, PROCESS_WEBHOOK_JOB)) == 2

    _run_one_process_webhook(database, hook_app[2])
    sync_jobs = _jobs(database, "sync_account")
    assert len(sync_jobs) == 1
    assert json.loads(sync_jobs[0].payload_json)["account_id"] == account_id
    processed = _events(database)[0]
    assert processed.account_id == account_id
    assert processed.subscription_id == subscription_id


def test_instagram_handshake_and_signed_delivery(
    client: TestClient, hook_app: tuple[FastAPI, Database, Settings]
) -> None:
    database = hook_app[1]

    good = client.get(
        "/hooks/instagram",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": IG_VERIFY_TOKEN,
            "hub.challenge": "ig-challenge",
        },
    )
    assert good.status_code == 200
    assert good.headers["content-type"].startswith("text/plain")
    assert good.text == "ig-challenge"

    wrong_token = client.get(
        "/hooks/instagram",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "not-the-token",
            "hub.challenge": "ig-challenge",
        },
    )
    assert wrong_token.status_code == 403

    account_id = _account(database, "instagram", "ig-external-1")
    body = json.dumps(
        {
            "object": "instagram",
            "entry": [{"id": "ig-external-1", "changes": [{"field": "comments"}]}],
        }
    ).encode()
    signature = "sha256=" + hmac.new(IG_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    delivered = client.post(
        "/hooks/instagram", content=body, headers={"X-Hub-Signature-256": signature}
    )
    assert delivered.status_code == 200

    _run_one_process_webhook(database, hook_app[2])
    sync_jobs = _jobs(database, "sync_account")
    assert len(sync_jobs) == 1
    assert json.loads(sync_jobs[0].payload_json)["account_id"] == account_id
    processed = _events(database)[0]
    assert processed.account_id == account_id
    assert processed.error is None


def test_a_body_over_one_megabyte_is_refused_and_nothing_is_stored(
    client: TestClient, hook_app: tuple[FastAPI, Database, Settings]
) -> None:
    database = hook_app[1]
    oversized = b"x" * (MAX_BODY_BYTES + 1)
    declared = client.post(
        "/hooks/tiktok",
        content=oversized,
        headers={"TikTok-Signature": "t=1,s=invalid"},
    )
    assert declared.status_code == 413
    assert _events(database) == []

    def chunks() -> Iterator[bytes]:
        yield b"y" * MAX_BODY_BYTES
        yield b"!"

    # No Content-Length (chunked transfer): the cap still applies while reading.
    chunked = client.post(
        "/hooks/tiktok",
        content=chunks(),
        headers={"TikTok-Signature": "t=1,s=invalid"},
    )
    assert chunked.status_code == 413
    assert _events(database) == []


def test_per_source_rate_limit_refuses_the_burst(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        tiktok_client_secret=TT_CLIENT_SECRET,
        webhook_rate_limit_per_minute=3,
    )
    database = initialize_database(settings)
    application = create_app(settings)
    try:
        client = TestClient(application, base_url="http://127.0.0.1:8000")
        statuses = [
            client.post(
                "/hooks/tiktok",
                content=b'{"event":"x"}' + str(index).encode(),
                headers={"TikTok-Signature": "t=1,s=bad"},
            ).status_code
            for index in range(4)
        ]
        assert statuses == [403, 403, 403, 429]
        # The over-limit request stored nothing.
        assert len(_events(database)) == 3
    finally:
        database.engine.dispose()
        application.state.database.engine.dispose()


def test_an_event_for_an_unknown_account_is_processed_and_dropped(
    client: TestClient, hook_app: tuple[FastAPI, Database, Settings]
) -> None:
    database = hook_app[1]
    body = json.dumps({"event": "user.profile.updated", "user_openid": "ghost"}).encode()
    response = client.post(
        "/hooks/tiktok",
        content=body,
        headers={"TikTok-Signature": _tiktok_signature(body)},
    )
    assert response.status_code == 200

    _run_one_process_webhook(database, hook_app[2])

    event = _events(database)[0]
    assert event.processed_at is not None
    assert event.error == "unknown account"
    assert event.account_id is None
    assert event.workspace_id is None
    assert _jobs(database, "sync_account") == []


def test_authorization_removed_marks_needs_reauth_and_cancels_pending_jobs(
    client: TestClient, hook_app: tuple[FastAPI, Database, Settings]
) -> None:
    database = hook_app[1]
    removed_id = _account(database, "tiktok", "openid-removed")
    other_id = _account(database, "tiktok", "openid-other")
    queue = JobQueue(database)
    own_job = queue.enqueue(
        "sync_account",
        idempotency_key="sync:removed",
        payload={"account_id": removed_id, "trigger": "scheduled"},
    )
    other_job = queue.enqueue(
        "sync_account",
        idempotency_key="sync:other",
        payload={"account_id": other_id, "trigger": "scheduled"},
    )

    body = json.dumps({"event": "authorization.removed", "user_openid": "openid-removed"}).encode()
    response = client.post(
        "/hooks/tiktok",
        content=body,
        headers={"TikTok-Signature": _tiktok_signature(body)},
    )
    assert response.status_code == 200

    _run_one_process_webhook(database, hook_app[2])

    with database.session() as session:
        account = session.get(ConnectedAccount, removed_id)
        assert account is not None
        assert account.status == "needs_reauth"
        own = session.get(PublishJob, own_job.id)
        other = session.get(PublishJob, other_job.id)
        assert own is not None and own.state == "cancelled"
        assert other is not None and other.state == "pending"
        account_events = session.scalars(
            select(EventRecord).where(EventRecord.type == "account.updated")
        ).all()
        assert len(account_events) == 1
        assert account_events[0].subject_type == "account"
        assert account_events[0].subject_id == str(removed_id)
    processed = _events(database)[0]
    assert processed.account_id == removed_id
    assert processed.processed_at is not None
    assert processed.error is None


def test_a_tiktok_publish_event_records_progress_without_a_target_lookup(
    client: TestClient, hook_app: tuple[FastAPI, Database, Settings]
) -> None:
    """`post.publish.*` hints: no target table on this branch, so only an event."""
    database = hook_app[1]
    _account(database, "tiktok", "openid-pub")
    body = json.dumps(
        {
            "event": "post.publish.complete",
            "user_openid": "openid-pub",
            "content": json.dumps({"publish_id": "pub-123"}),
        }
    ).encode()
    response = client.post(
        "/hooks/tiktok",
        content=body,
        headers={"TikTok-Signature": _tiktok_signature(body)},
    )
    assert response.status_code == 200

    _run_one_process_webhook(database, hook_app[2])

    with database.session() as session:
        progress = session.scalars(
            select(EventRecord).where(EventRecord.type == "target.progress")
        ).all()
        assert len(progress) == 1
        payload = json.loads(progress[0].payload_json)
        assert payload == {"event": "post.publish.complete", "publish_id": "pub-123"}
        assert progress[0].subject_type == "account"
    assert _jobs(database, "sync_account") == []


def test_a_verified_publish_webhook_enqueues_a_poll_for_the_matching_target(
    client: TestClient, hook_app: tuple[FastAPI, Database, Settings]
) -> None:
    """A `post.publish.*` hint for a known `publish_id` queues an immediate poll."""
    database = hook_app[1]
    account_id = _account(database, "tiktok", "openid-pub")
    with database.session() as session:
        draft = models.ContentDraft(title="Clip", body="", status="draft")
        session.add(draft)
        session.flush()
        target = models.DraftTarget(
            draft_id=draft.id,
            platform="tiktok",
            account_id=account_id,
            status="pending",
            state="processing",
            external_id="pub-123",
        )
        session.add(target)
        session.flush()
        target_id = target.id
    body = json.dumps(
        {
            "event": "post.publish.complete",
            "user_openid": "openid-pub",
            "content": json.dumps({"publish_id": "pub-123"}),
        }
    ).encode()
    response = client.post(
        "/hooks/tiktok",
        content=body,
        headers={"TikTok-Signature": _tiktok_signature(body)},
    )
    assert response.status_code == 200

    _run_one_process_webhook(database, hook_app[2])

    poll_jobs = _jobs(database, "poll_processing")
    assert len(poll_jobs) == 1
    assert poll_jobs[0].target_id == target_id
    assert json.loads(poll_jobs[0].payload_json) == {
        "via": "webhook",
        "event": "post.publish.complete",
    }
    # A matched hint queues the poll; it does not also record account progress.
    with database.session() as session:
        assert (
            session.scalars(select(EventRecord).where(EventRecord.type == "target.progress")).all()
            == []
        )
    assert _jobs(database, "sync_account") == []


def test_an_unparsable_body_is_recorded_and_dropped(
    client: TestClient, hook_app: tuple[FastAPI, Database, Settings]
) -> None:
    database = hook_app[1]
    body = b"not json {"
    response = client.post(
        "/hooks/tiktok",
        content=body,
        headers={"TikTok-Signature": _tiktok_signature(body)},
    )
    assert response.status_code == 200

    _run_one_process_webhook(database, hook_app[2])

    event = _events(database)[0]
    assert event.error == "unparsable body"
    assert event.processed_at is not None
    assert _jobs(database, "sync_account") == []


def test_the_purge_removes_only_webhook_events_older_than_30_days(
    hook_app: tuple[FastAPI, Database, Settings],
) -> None:
    _application, database, settings = hook_app
    now = utcnow()
    with database.session() as session:
        old = models.WebhookEvent(
            provider="tiktok",
            received_at=now - timedelta(days=31),
            signature_ok=True,
            body=b"old",
        )
        recent = models.WebhookEvent(
            provider="tiktok",
            received_at=now - timedelta(days=29),
            signature_ok=False,
            body=b"recent",
        )
        session.add_all([old, recent])
        session.flush()
        old_id, recent_id = old.id, recent.id

    from runway.jobs.retention import run_purge_job

    job = Job.of(
        PublishJob(
            id=1,
            kind="purge",
            workspace_id=1,
            target_id=None,
            state="running",
            attempts=1,
            max_attempts=1,
            run_at=now,
            idempotency_key="purge:test",
            payload_json="{}",
            last_error=None,
        )
    )
    run_purge_job(database, settings, job)

    remaining = [event.id for event in _events(database)]
    assert remaining == [recent_id]
    assert old_id not in remaining


def test_process_webhook_jobs_are_registered_with_the_worker(
    hook_app: tuple[FastAPI, Database, Settings],
) -> None:
    worker: Worker = build_worker(hook_app[2], hook_app[1])
    assert PROCESS_WEBHOOK_JOB in worker.handlers


# ------------------------------------------------------------- review fixes


def test_the_websub_callback_requires_its_unguessable_token(
    client: TestClient, hook_app: tuple[FastAPI, Database, Settings]
) -> None:
    """The callback token is part of the URL: without it the route is a 404.

    A wrong or missing token reveals nothing and changes nothing — not even an
    unverified `webhook_events` row is stored, so a guessed subscription id
    cannot be probed or polluted.
    """
    database = hook_app[1]
    account_id = _account(database, "youtube", "UC-test")
    subscription_id = _subscription(database, account_id)

    good_params = {
        "hub.mode": "subscribe",
        "hub.topic": YOUTUBE_TOPIC,
        "hub.challenge": "challenge-abc",
        "hub.lease_seconds": "432000",
    }
    wrong_token = client.get(
        f"/hooks/youtube/websub/{subscription_id}/{'cd' * 32}",
        params=good_params,
    )
    assert wrong_token.status_code == 404
    # The pre-token route shape is gone entirely.
    old_path = client.get(f"/hooks/youtube/websub/{subscription_id}", params=good_params)
    assert old_path.status_code == 404

    sha1 = "sha1=" + hmac.new(WEBSUB_SECRET.encode(), ATOM_FEED, hashlib.sha1).hexdigest()
    posted_wrong = client.post(
        f"/hooks/youtube/websub/{subscription_id}/{'cd' * 32}",
        content=ATOM_FEED,
        headers={"X-Hub-Signature": sha1},
    )
    assert posted_wrong.status_code == 404
    posted_old = client.post(
        f"/hooks/youtube/websub/{subscription_id}",
        content=ATOM_FEED,
        headers={"X-Hub-Signature": sha1},
    )
    assert posted_old.status_code == 404

    assert _events(database) == []
    with database.session() as session:
        subscription = session.get(models.PlatformSubscription, subscription_id)
        assert subscription is not None
        assert subscription.state == "pending"
        assert subscription.subscribed_at is None


def test_a_non_ascii_callback_token_is_a_404_not_a_500(
    client: TestClient, hook_app: tuple[FastAPI, Database, Settings]
) -> None:
    database = hook_app[1]
    account_id = _account(database, "youtube", "UC-test")
    subscription_id = _subscription(database, account_id)

    response = client.get(
        f"/hooks/youtube/websub/{subscription_id}/tok\u00e9n",
        params={"hub.mode": "subscribe", "hub.topic": YOUTUBE_TOPIC, "hub.challenge": "c"},
    )
    assert response.status_code == 404


def test_non_ascii_signature_headers_and_verify_tokens_are_unverified(
    client: TestClient, hook_app: tuple[FastAPI, Database, Settings]
) -> None:
    """Non-ASCII material compares as bytes: unverified/403, never a 500."""
    database = hook_app[1]
    account_id = _account(database, "youtube", "UC-test")
    subscription_id = _subscription(database, account_id)

    # Header values go over the wire as bytes; httpx only refuses str values
    # outside ASCII, so the raw byte form is how a non-ASCII signature arrives.
    tiktok = client.post(
        "/hooks/tiktok",
        content=b'{"event":"x"}',
        headers={b"tiktok-signature": b"t=" + str(int(time.time())).encode() + b",s=\xe9\xe9\xe9"},
    )
    assert tiktok.status_code == 403
    websub = client.post(
        f"/hooks/youtube/websub/{subscription_id}/{CALLBACK_TOKEN}",
        content=ATOM_FEED,
        headers={b"x-hub-signature": b"sha256=\xe9\xe9"},
    )
    assert websub.status_code == 403
    instagram = client.post(
        "/hooks/instagram",
        content=b"{}",
        headers={b"x-hub-signature-256": b"sha256=\xe9\xe9"},
    )
    assert instagram.status_code == 403
    handshake = client.get(
        "/hooks/instagram",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "tok\u00e9n",
            "hub.challenge": "c",
        },
    )
    assert handshake.status_code == 403

    events = _events(database)
    assert len(events) == 3
    assert [event.signature_ok for event in events] == [False, False, False]


def test_stored_content_type_and_source_are_truncated_to_their_columns(
    hook_app: tuple[FastAPI, Database, Settings],
) -> None:
    application, database, _settings = hook_app
    long_source = "x" * 90
    long_type = "application/" + "y" * 200
    client = TestClient(application, base_url="http://127.0.0.1:8000", client=(long_source, 9))

    response = client.post(
        "/hooks/tiktok",
        content=b'{"event":"x"}',
        headers={
            "TikTok-Signature": "t=1,s=bad",
            "Content-Type": long_type,
        },
    )
    assert response.status_code == 403  # stored unverified, not rejected
    event = _events(database)[0]
    assert event.content_type == long_type[:100]
    assert event.source == long_source[:64]


def _edge_client(
    application: FastAPI, *, peer: str = "127.0.0.1", base_url: str = "https://edge.example.com"
) -> TestClient:
    return TestClient(application, base_url=base_url, client=(peer, 4321))


def test_rate_limit_keys_on_left_most_forwarded_for_only_via_the_edge(
    tmp_path: Path,
) -> None:
    """Behind the public edge the real client is the left-most X-Forwarded-For.

    Tailscale Funnel terminates the public request and forwards it from a
    loopback peer with XFF set; each remote source then gets its own bucket.
    The same header on a direct or non-loopback request is spoofable and is
    never trusted.
    """
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        tiktok_client_secret=TT_CLIENT_SECRET,
        webhook_rate_limit_per_minute=2,
        public_edge_url="https://edge.example.com",
    )
    database = initialize_database(settings)
    application = create_app(settings)

    def post(client: TestClient, xff: str | None) -> int:
        headers = {"TikTok-Signature": "t=1,s=bad"}
        if xff is not None:
            headers["X-Forwarded-For"] = xff
        return client.post("/hooks/tiktok", content=b'{"event":"x"}', headers=headers).status_code

    try:
        # The funnel's own shape: loopback peer, edge Host.
        funnel = _edge_client(application)
        assert [post(funnel, "203.0.113.1") for _ in range(2)] == [403, 403]
        assert post(funnel, "203.0.113.1") == 429  # third hit in the window
        assert post(funnel, "203.0.113.2") == 403  # a different client: fresh bucket
        # A proxy chain keys on the left-most (original) address: changing the
        # right side stays in the same bucket, so it still hits the limit.
        assert [post(funnel, "203.0.113.3, 10.0.0.9") for _ in range(2)] == [403, 403]
        assert post(funnel, "203.0.113.3, 10.0.0.8") == 429

        # A non-loopback peer claiming the edge host cannot name its source.
        stranger = _edge_client(application, peer="10.9.9.9")
        assert [post(stranger, "198.51.100.1") for _ in range(2)] == [403, 403]
        assert post(stranger, "198.51.100.2") == 429  # still the peer's bucket

        # Direct requests ignore XFF entirely.
        direct = TestClient(
            application, base_url="http://127.0.0.1:8000", client=("203.0.113.7", 9)
        )
        assert [post(direct, "198.51.100.5") for _ in range(2)] == [403, 403]
        assert post(direct, "198.51.100.6") == 429
    finally:
        database.engine.dispose()
        application.state.database.engine.dispose()
