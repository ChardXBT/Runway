"""YouTube WebSub lifecycle: subscribe on connect, renew at 80% of the granted
lease, unsubscribe on disconnect (spec 04 sections 2 and 5, RT3 item 1).

A connect (only when a public callback base exists) creates a
`platform_subscriptions` row and enqueues `renew_subscription`; the worker
posts the subscribe/unsubscribe form to the hub through an injected HTTP
client; a producer renews at 80% of the granted lease. Tests use fake
providers and a recording fake hub; nothing touches the network.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from runway.api.app import create_app
from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import (
    ConnectedAccount,
    PlatformSubscription,
    PublishJob,
    utcnow,
)
from runway.jobs.queue import Job, JobQueue
from runway.jobs.runtime import build_worker
from runway.platforms.base import (
    AccountIdentity,
    ProviderError,
    TokenSet,
)
from runway.platforms.capabilities import Platform
from runway.platforms.registry import ProviderRegistry
from runway.platforms.youtube import YouTubeProvider
from runway.realtime import subscriptions
from runway.realtime.subscriptions import (
    RENEW_SUBSCRIPTION_JOB,
    WEBSUB_HUB_URL,
    SubscriptionScheduler,
    run_renew_subscription_job,
)

EDGE = "https://edge.example.com"
CHANNEL = "UC-test"
YOUTUBE_TOPIC = f"https://www.youtube.com/xml/feeds/videos.xml?channel_id={CHANNEL}"


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    base: dict[str, object] = {
        "data_dir": tmp_path / "data",
        "platform_provider": "live",
        "youtube_client_id": "yt-client",
        "youtube_client_secret": "yt-secret",
    }
    base.update(overrides)
    return Settings(**base)


class _FakeYouTubeProvider(YouTubeProvider):
    """Real provider shell; the OAuth legs are stubbed, no HTTP happens."""

    def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None,
    ) -> TokenSet:
        return TokenSet(
            access_token="yt-access",
            refresh_token="yt-refresh",
            expires_at=datetime.now(UTC) + timedelta(hours=1),
            scopes=["scope.readonly"],
        )

    def fetch_identity(self, token: TokenSet) -> AccountIdentity:
        return AccountIdentity(
            external_account_id=CHANNEL,
            handle="@northwind",
            raw={"contentDetails": {"relatedPlaylists": {"uploads": "UUtest"}}},
        )


def _patch_youtube(monkeypatch: pytest.MonkeyPatch, provider: _FakeYouTubeProvider) -> None:
    original = ProviderRegistry.provider

    def patched(self: ProviderRegistry, platform: Platform) -> Any:
        if platform is Platform.youtube:
            return provider
        return original(self, platform)

    monkeypatch.setattr(ProviderRegistry, "provider", patched)


def _connect_youtube(settings: Settings) -> None:
    """Drive the real connect + callback routes through a fake provider."""
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1:8000") as web:
        connect = web.post("/api/platforms/youtube/connect")
        assert connect.status_code == 200
        state = str(connect.json()["state"])
        callback = web.get(
            "/api/platforms/youtube/oauth/callback",
            params={"code": "auth-code", "state": state},
            follow_redirects=False,
        )
        assert callback.status_code == 303, callback.text
        assert callback.headers["location"].endswith("/accounts?connected=youtube")
    app.state.database.engine.dispose()


def _account(database: Database, platform: str = "youtube", external_id: str = CHANNEL) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform=platform,
            external_account_id=external_id,
            status="connected",
            granted_scopes_json="[]",
        )
        session.add(account)
        session.flush()
        return int(account.id)


def _subscription(database: Database, **fields: Any) -> PlatformSubscription:
    with database.session() as session:
        subscription = PlatformSubscription(
            account_id=fields.pop("account_id"),
            provider="youtube",
            topic=fields.pop("topic", YOUTUBE_TOPIC),
            hub_secret=fields.pop("hub_secret", "aa" * 32),
            callback_token=fields.pop("callback_token", "bb" * 32),
            **fields,
        )
        session.add(subscription)
        session.flush()
        session.expunge(subscription)
        return subscription


def _get_subscription(database: Database, subscription_id: int) -> PlatformSubscription:
    with database.session() as session:
        subscription = session.get(PlatformSubscription, subscription_id)
        assert subscription is not None
        session.expunge(subscription)
        return subscription


def _jobs(database: Database, kind: str) -> list[PublishJob]:
    with database.session() as session:
        rows = session.scalars(
            select(PublishJob).where(PublishJob.kind == kind).order_by(PublishJob.id)
        ).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def _claim(database: Database, kind: str) -> Job:
    job = JobQueue(database).claim("w-test", {kind})
    assert job is not None, f"expected a pending {kind} job"
    return job


def _recording_post(
    calls: list[tuple[str, dict[str, str]]], status: int = 202
) -> Callable[[str, dict[str, str]], int]:
    def post(url: str, fields: dict[str, str]) -> int:
        calls.append((url, dict(fields)))
        return status

    return post


# ---------------------------------------------------------------- connect


def test_a_youtube_connect_creates_a_pending_subscription_and_subscribe_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path, public_edge_url=EDGE)
    database = initialize_database(settings)
    _patch_youtube(monkeypatch, _FakeYouTubeProvider(settings))
    try:
        _connect_youtube(settings)

        with database.session() as session:
            subscription = session.scalar(
                select(PlatformSubscription).where(PlatformSubscription.provider == "youtube")
            )
            assert subscription is not None
            assert subscription.state == "pending"
            assert subscription.topic == YOUTUBE_TOPIC
            assert subscription.hub_secret is not None and len(subscription.hub_secret) == 64
            assert (
                subscription.callback_token is not None and len(subscription.callback_token) == 64
            )
            jobs = session.scalars(
                select(PublishJob).where(PublishJob.kind == RENEW_SUBSCRIPTION_JOB)
            ).all()
            assert len(jobs) == 1
            assert json.loads(jobs[0].payload_json) == {
                "subscription_id": subscription.id,
                "mode": "subscribe",
            }
    finally:
        database.engine.dispose()


def test_a_youtube_connect_without_a_public_base_subscribes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    _patch_youtube(monkeypatch, _FakeYouTubeProvider(settings))
    try:
        _connect_youtube(settings)

        with database.session() as session:
            assert session.scalars(select(PlatformSubscription)).all() == []
            assert (
                session.scalars(
                    select(PublishJob).where(PublishJob.kind == RENEW_SUBSCRIPTION_JOB)
                ).all()
                == []
            )
    finally:
        database.engine.dispose()


# -------------------------------------------------------------- the job


def test_the_subscribe_job_posts_the_websub_hub_form(tmp_path: Path) -> None:
    settings = _settings(tmp_path, public_edge_url=EDGE)
    database = initialize_database(settings)
    queue = JobQueue(database)
    try:
        account_id = _account(database)
        with database.session() as session:
            account = session.get(ConnectedAccount, account_id)
            assert account is not None
            subscriptions.on_account_connected(session, queue, settings, account)
            subscription_id = int(session.scalar(select(PlatformSubscription.id)) or 0)

        calls: list[tuple[str, dict[str, str]]] = []
        run_renew_subscription_job(
            database,
            settings,
            queue,
            _claim(database, RENEW_SUBSCRIPTION_JOB),
            http_post=_recording_post(calls, 202),
        )

        subscription = _get_subscription(database, subscription_id)
        assert calls == [
            (
                WEBSUB_HUB_URL,
                {
                    "hub.callback": (
                        f"{EDGE}/hooks/youtube/websub/{subscription_id}/"
                        f"{subscription.callback_token}"
                    ),
                    "hub.topic": YOUTUBE_TOPIC,
                    "hub.mode": "subscribe",
                    "hub.verify": "async",
                    "hub.secret": subscription.hub_secret or "",
                    "hub.lease_seconds": "432000",
                },
            )
        ]
        # Accepted is not yet subscribed: the hub's verification GET records
        # the granted lease and flips the state.
        assert subscription.state == "pending"
        assert subscription.last_error is None
    finally:
        database.engine.dispose()


def test_a_hub_rejection_marks_the_subscription_failed_and_retries(tmp_path: Path) -> None:
    settings = _settings(tmp_path, public_edge_url=EDGE)
    database = initialize_database(settings)
    queue = JobQueue(database)
    try:
        subscription = _subscription(database, account_id=_account(database), state="pending")
        job = _job_row(subscription.id)

        calls: list[tuple[str, dict[str, str]]] = []
        with pytest.raises(ProviderError):
            run_renew_subscription_job(
                database,
                settings,
                queue,
                job,
                http_post=_recording_post(calls, 500),
            )

        subscription = _get_subscription(database, subscription.id)
        assert subscription.state == "failed"
        assert subscription.last_error == "hub returned HTTP 500"
    finally:
        database.engine.dispose()


def _job_row(subscription_id: int, mode: str = "subscribe") -> Job:
    """A renew job as the worker would claim it, without enqueueing."""
    return Job.of(
        PublishJob(
            id=1,
            kind=RENEW_SUBSCRIPTION_JOB,
            workspace_id=1,
            target_id=None,
            state="running",
            attempts=1,
            max_attempts=1,
            run_at=utcnow(),
            idempotency_key=f"test:{subscription_id}:{mode}",
            payload_json=json.dumps({"subscription_id": subscription_id, "mode": mode}),
            last_error=None,
        )
    )


# ---------------------------------------------------------------- renewals


def test_the_producer_renews_at_eighty_percent_of_the_lease(tmp_path: Path) -> None:
    database = initialize_database(_settings(tmp_path, public_edge_url=EDGE))
    queue = JobQueue(database)
    subscribed_at = datetime(2026, 1, 1, tzinfo=UTC)
    due = subscribed_at + timedelta(seconds=432000 * 0.8)
    try:
        subscription = _subscription(
            database,
            account_id=_account(database),
            state="active",
            lease_seconds=432000,
            subscribed_at=subscribed_at,
            expires_at=subscribed_at + timedelta(seconds=432000),
        )
        scheduler = SubscriptionScheduler(database, queue)

        scheduler.produce(due - timedelta(seconds=1))
        assert _jobs(database, RENEW_SUBSCRIPTION_JOB) == []

        scheduler.produce(due)
        jobs = _jobs(database, RENEW_SUBSCRIPTION_JOB)
        assert len(jobs) == 1
        assert json.loads(jobs[0].payload_json) == {
            "subscription_id": subscription.id,
            "mode": "subscribe",
        }

        # Producing again for the same due instant never duplicates.
        scheduler.produce(due)
        assert len(_jobs(database, RENEW_SUBSCRIPTION_JOB)) == 1
    finally:
        database.engine.dispose()


def test_an_expired_lease_is_marked_expired_and_retried(tmp_path: Path) -> None:
    database = initialize_database(_settings(tmp_path, public_edge_url=EDGE))
    queue = JobQueue(database)
    now = utcnow()
    try:
        subscription = _subscription(
            database,
            account_id=_account(database),
            state="active",
            lease_seconds=432000,
            subscribed_at=now - timedelta(days=6),
            expires_at=now - timedelta(hours=1),
        )
        scheduler = SubscriptionScheduler(database, queue)
        scheduler.produce(now)

        subscription = _get_subscription(database, subscription.id)
        assert subscription.state == "expired"
        jobs = _jobs(database, RENEW_SUBSCRIPTION_JOB)
        assert len(jobs) == 1
        assert json.loads(jobs[0].payload_json)["mode"] == "subscribe"
    finally:
        database.engine.dispose()


# ---------------------------------------------------------------- disconnect


def test_disconnect_marks_unsubscribed_and_the_job_posts_the_unsubscribe_form(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, public_edge_url=EDGE)
    database = initialize_database(settings)
    queue = JobQueue(database)
    try:
        account_id = _account(database)
        with database.session() as session:
            account = session.get(ConnectedAccount, account_id)
            assert account is not None
            subscriptions.on_account_connected(session, queue, settings, account)
            subscriptions.on_account_disconnected(session, queue, account)

        subscription = _get_subscription(database, _subscription_id(database))
        assert subscription.state == "unsubscribed"
        # The pending subscribe from connect was cancelled; only the
        # unsubscribe job remains.
        jobs = _jobs(database, RENEW_SUBSCRIPTION_JOB)
        pending = [job for job in jobs if job.state == "pending"]
        assert len(pending) == 1
        assert json.loads(pending[0].payload_json)["mode"] == "unsubscribe"

        calls: list[tuple[str, dict[str, str]]] = []
        run_renew_subscription_job(
            database,
            settings,
            queue,
            _claim(database, RENEW_SUBSCRIPTION_JOB),
            http_post=_recording_post(calls, 204),
        )
        url, fields = calls[0]
        assert url == WEBSUB_HUB_URL
        assert fields["hub.mode"] == "unsubscribe"
        assert fields["hub.verify"] == "async"
        assert fields["hub.callback"] == (
            f"{EDGE}/hooks/youtube/websub/{subscription.id}/{subscription.callback_token}"
        )
        assert fields["hub.topic"] == YOUTUBE_TOPIC
        assert "hub.secret" not in fields
        assert "hub.lease_seconds" not in fields
    finally:
        database.engine.dispose()


def _subscription_id(database: Database) -> int:
    with database.session() as session:
        return int(session.scalar(select(PlatformSubscription.id)) or 0)


def test_disconnect_route_enqueues_unsubscribe_for_a_youtube_subscription(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, public_edge_url=EDGE)
    database = initialize_database(settings)
    queue = JobQueue(database)
    try:
        account_id = _account(database)
        with database.session() as session:
            account = session.get(ConnectedAccount, account_id)
            assert account is not None
            subscriptions.on_account_connected(session, queue, settings, account)

        app = create_app(settings)
        with TestClient(app, base_url="http://127.0.0.1:8000") as web:
            response = web.post(
                f"/api/platforms/accounts/{account_id}/disconnect",
                json={"confirmed": True},
            )
            assert response.status_code == 200
        app.state.database.engine.dispose()

        subscription = _get_subscription(database, _subscription_id(database))
        assert subscription.state == "unsubscribed"
        modes = [
            json.loads(job.payload_json)["mode"]
            for job in _jobs(database, RENEW_SUBSCRIPTION_JOB)
            if job.state == "pending"
        ]
        assert modes == ["unsubscribe"]
    finally:
        database.engine.dispose()


def test_renew_subscription_is_registered_with_the_worker(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    try:
        worker = build_worker(settings, database)
        assert RENEW_SUBSCRIPTION_JOB in worker.handlers
        assert any(type(p) is SubscriptionScheduler for p in worker.producers)
    finally:
        database.engine.dispose()
