"""Instagram long-lived token upkeep (spec 04 section 2, RT3 item 3).

An Instagram Login token lives about 60 days, can only be refreshed while
it is still valid, and Meta refuses to refresh one younger than 24 hours.
A producer therefore enqueues a ``refresh_token`` job at about day 50 of
the stored token's life; the handler refreshes through
``ProviderRegistry.fresh_token`` (one refresh under the token lock), marks
the account ``needs_reauth`` on a dead grant and lets a transient failure
retry. No network: the provider is a fake.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import ConnectedAccount, PublishJob
from runway.jobs.queue import Job, JobQueue
from runway.jobs.runtime import build_worker
from runway.platforms.base import (
    AuthorizationExpired,
    ProviderError,
    TokenSet,
)
from runway.platforms.capabilities import Platform
from runway.platforms.instagram import InstagramProvider
from runway.platforms.registry import ProviderRegistry
from runway.realtime.token_upkeep import (
    INSTAGRAM_REFRESH_AT_AGE,
    REFRESH_TOKEN_JOB,
    TokenUpkeepScheduler,
    run_refresh_token_job,
)

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path / "data",
        platform_provider="live",
        instagram_app_id="ig-app",
        instagram_app_secret="ig-secret",
    )


class _FakeRefreshProvider(InstagramProvider):
    """Records refresh calls; returns or raises whatever it was given."""

    def __init__(
        self,
        settings: Settings,
        *,
        refreshed: TokenSet | None = None,
        error: ProviderError | None = None,
    ) -> None:
        super().__init__(settings)
        self.refresh_calls = 0
        self._refreshed = refreshed
        self._error = error

    def refresh(self, token: TokenSet) -> TokenSet:
        self.refresh_calls += 1
        if self._error is not None:
            raise self._error
        if self._refreshed is not None:
            return self._refreshed
        return TokenSet(
            access_token="fresh-ig",
            expires_at=NOW + timedelta(days=60),
            extra={"issued_at": NOW.isoformat()},
        )


def _account(
    database: Database,
    *,
    token_ref: str | None = "ref-1",
    status: str = "connected",
    platform: str = "instagram",
) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform=platform,
            external_account_id="ext-1",
            status=status,
            granted_scopes_json="[]",
            token_ref=token_ref,
        )
        session.add(account)
        session.flush()
        return int(account.id)


def _token(
    *,
    issued_at: datetime | None = NOW - timedelta(days=50),
    expires_at: datetime | None = NOW + timedelta(days=10),
) -> TokenSet:
    extra = {"issued_at": issued_at.isoformat()} if issued_at is not None else {}
    return TokenSet(access_token="old-ig", expires_at=expires_at, extra=extra)


def _store(registry: ProviderRegistry, token_ref: str, token: TokenSet) -> None:
    registry.token_store.store(token_ref, token)


def _job(account_id: int) -> Job:
    return Job.of(
        PublishJob(
            id=1,
            kind=REFRESH_TOKEN_JOB,
            workspace_id=1,
            target_id=None,
            state="running",
            attempts=1,
            max_attempts=3,
            run_at=NOW,
            idempotency_key="test:refresh",
            payload_json=json.dumps({"account_id": account_id}),
            last_error=None,
        )
    )


def _pending(database: Database, kind: str) -> list[PublishJob]:
    with database.session() as session:
        rows = session.scalars(
            select(PublishJob).where(PublishJob.kind == kind).order_by(PublishJob.id)
        ).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


# ------------------------------------------------------------ the producer


def test_a_fifty_day_old_token_gets_a_due_refresh_job(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    try:
        account_id = _account(database)
        _store(registry, "ref-1", _token())
        scheduler = TokenUpkeepScheduler(database, registry, JobQueue(database))
        scheduler._last_scan = NOW - timedelta(hours=1)

        scheduler.produce(NOW)

        jobs = _pending(database, REFRESH_TOKEN_JOB)
        assert len(jobs) == 1
        assert json.loads(jobs[0].payload_json) == {"account_id": account_id}
        assert _aware(jobs[0].run_at) <= NOW
    finally:
        database.engine.dispose()


def test_a_younger_token_is_scheduled_at_exactly_day_50(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    try:
        issued = NOW - timedelta(days=49, hours=7)
        _account(database)
        _store(registry, "ref-1", _token(issued_at=issued))
        scheduler = TokenUpkeepScheduler(database, registry, JobQueue(database))
        scheduler._last_scan = NOW - timedelta(hours=1)

        scheduler.produce(NOW)

        jobs = _pending(database, REFRESH_TOKEN_JOB)
        assert len(jobs) == 1
        assert _aware(jobs[0].run_at) == issued + INSTAGRAM_REFRESH_AT_AGE
    finally:
        database.engine.dispose()


def test_the_expiry_fallback_derives_the_issue_time(tmp_path: Path) -> None:
    """A token stored before `issued_at` existed still gets its day-50 job."""
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    try:
        _account(database)
        # No issued_at: expires in 10 days, so it is about day 50 of 60.
        _store(registry, "ref-1", _token(issued_at=None, expires_at=NOW + timedelta(days=10)))
        scheduler = TokenUpkeepScheduler(database, registry, JobQueue(database))
        scheduler._last_scan = NOW - timedelta(hours=1)

        scheduler.produce(NOW)

        assert len(_pending(database, REFRESH_TOKEN_JOB)) == 1
    finally:
        database.engine.dispose()


def test_producing_twice_never_duplicates(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    try:
        _account(database)
        _store(registry, "ref-1", _token())
        scheduler = TokenUpkeepScheduler(
            database, registry, JobQueue(database), interval=timedelta(0)
        )

        scheduler.produce(NOW)
        scheduler.produce(NOW + timedelta(minutes=5))

        assert len(_pending(database, REFRESH_TOKEN_JOB)) == 1
    finally:
        database.engine.dispose()


def test_disconnected_and_other_platforms_are_ignored(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    try:
        _account(database, token_ref="ref-1", status="disconnected")
        _account(database, token_ref="ref-2", platform="youtube")
        _store(registry, "ref-1", _token())
        _store(registry, "ref-2", _token())
        scheduler = TokenUpkeepScheduler(
            database, registry, JobQueue(database), interval=timedelta(0)
        )

        scheduler.produce(NOW)

        assert _pending(database, REFRESH_TOKEN_JOB) == []
    finally:
        database.engine.dispose()


# ------------------------------------------------------------- the handler


def test_a_due_token_is_refreshed_and_stored(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    provider = _FakeRefreshProvider(settings)
    registry.set_provider(Platform.instagram, provider)
    try:
        account_id = _account(database)
        _store(registry, "ref-1", _token())

        run_refresh_token_job(registry, _job(account_id), clock=lambda: NOW)

        assert provider.refresh_calls == 1
        stored = registry.token_store.load("ref-1")
        assert stored is not None
        assert stored.access_token == "fresh-ig"
        with database.session() as session:
            assert session.get(ConnectedAccount, account_id).status == "connected"  # type: ignore[union-attr]
    finally:
        database.engine.dispose()


def test_a_token_under_24h_is_never_refreshed(tmp_path: Path) -> None:
    """Meta refuses to refresh a token younger than 24 h, so the job must not
    even ask - a queued job can fire while a newer token was just stored."""
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    provider = _FakeRefreshProvider(settings)
    registry.set_provider(Platform.instagram, provider)
    try:
        account_id = _account(database)
        # Issued 10 h ago yet expiring inside the 30-day window: without the
        # age guard `fresh_token` would refresh it right now.
        _store(
            registry,
            "ref-1",
            _token(
                issued_at=NOW - timedelta(hours=10),
                expires_at=NOW + timedelta(days=5),
            ),
        )

        run_refresh_token_job(registry, _job(account_id), clock=lambda: NOW)

        assert provider.refresh_calls == 0
        stored = registry.token_store.load("ref-1")
        assert stored is not None
        assert stored.access_token == "old-ig"
    finally:
        database.engine.dispose()


def test_a_transient_failure_keeps_the_account_connected(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    provider = _FakeRefreshProvider(settings, error=ProviderError("Meta 500", retryable=True))
    registry.set_provider(Platform.instagram, provider)
    try:
        account_id = _account(database)
        _store(registry, "ref-1", _token())

        with pytest.raises(ProviderError):
            run_refresh_token_job(registry, _job(account_id), clock=lambda: NOW)

        with database.session() as session:
            assert session.get(ConnectedAccount, account_id).status == "connected"  # type: ignore[union-attr]
    finally:
        database.engine.dispose()


def test_a_dead_grant_marks_needs_reauth(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    provider = _FakeRefreshProvider(
        settings, error=AuthorizationExpired("token is expired", status=401)
    )
    registry.set_provider(Platform.instagram, provider)
    try:
        account_id = _account(database)
        _store(registry, "ref-1", _token())

        run_refresh_token_job(registry, _job(account_id), clock=lambda: NOW)

        with database.session() as session:
            account = session.get(ConnectedAccount, account_id)
            assert account is not None
            assert account.status == "needs_reauth"
            assert account.last_error
    finally:
        database.engine.dispose()


def test_refresh_token_is_registered_with_the_worker(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    try:
        worker = build_worker(settings, database)
        assert REFRESH_TOKEN_JOB in worker.handlers
    finally:
        database.engine.dispose()
