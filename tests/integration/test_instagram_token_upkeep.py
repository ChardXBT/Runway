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
from runway.db.models import ConnectedAccount, EventRecord, PublishJob
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
    external_id: str = "ext-1",
) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform=platform,
            external_account_id=external_id,
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


def test_a_failed_scan_does_not_suppress_the_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_last_scan`` is stamped only after the scan succeeds: a session
    that blows up leaves the scheduler due immediately, so the next
    produce retries instead of sleeping the hourly interval away."""
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    try:
        _account(database)
        _store(registry, "ref-1", _token())
        scheduler = TokenUpkeepScheduler(database, registry, JobQueue(database))
        real_session = database.session

        def _broken_session() -> None:
            raise RuntimeError("db down")

        monkeypatch.setattr(database, "session", _broken_session)
        with pytest.raises(RuntimeError):
            scheduler.produce(NOW)
        assert scheduler._last_scan is None
        assert scheduler.next_due(NOW) == NOW

        monkeypatch.setattr(database, "session", real_session)
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


# ------------------------------------------------- refresh-expiry warning
#
# Spec 04 section 2 (RT3 item 4): an account whose refresh token has a
# known end (`extra["refresh_expires_at"]`, TikTok records it) gets one
# `account.updated` event {"warning": "refresh_token_expiring",
# "days_left": n} once 14 days or fewer remain - at most once per account
# per day, and only while the token is still alive.


def _events(database: Database) -> list[tuple[str | None, dict]]:
    with database.session() as session:
        rows = session.scalars(
            select(EventRecord).where(EventRecord.type == "account.updated")
        ).all()
        return [(row.subject_id, json.loads(row.payload_json or "{}")) for row in rows]


def _tiktok_token(*, refresh_expires_at: datetime | None) -> TokenSet:
    extra = (
        {"refresh_expires_at": refresh_expires_at.isoformat()}
        if refresh_expires_at is not None
        else {}
    )
    return TokenSet(
        access_token="tt-access",
        refresh_token="tt-refresh",
        expires_at=NOW + timedelta(hours=12),
        extra=extra,
    )


def test_a_refresh_token_inside_14_days_warns(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    try:
        account_id = _account(database, token_ref="ref-1", platform="tiktok")
        _store(registry, "ref-1", _tiktok_token(refresh_expires_at=NOW + timedelta(days=13)))
        scheduler = TokenUpkeepScheduler(
            database, registry, JobQueue(database), interval=timedelta(0)
        )

        scheduler.produce(NOW)

        events = _events(database)
        assert events == [
            (
                str(account_id),
                {"warning": "refresh_token_expiring", "days_left": 13},
            )
        ]
    finally:
        database.engine.dispose()


def test_a_refresh_token_outside_14_days_warns_nothing(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    try:
        _account(database, token_ref="ref-1", platform="tiktok")
        _store(registry, "ref-1", _tiktok_token(refresh_expires_at=NOW + timedelta(days=15)))
        scheduler = TokenUpkeepScheduler(
            database, registry, JobQueue(database), interval=timedelta(0)
        )

        scheduler.produce(NOW)

        assert _events(database) == []
    finally:
        database.engine.dispose()


def test_a_dead_refresh_token_warns_nothing(tmp_path: Path) -> None:
    """An already-expired refresh grant shows as needs_reauth, not a warning."""
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    try:
        _account(database, token_ref="ref-1", platform="tiktok")
        _store(registry, "ref-1", _tiktok_token(refresh_expires_at=NOW - timedelta(hours=1)))
        scheduler = TokenUpkeepScheduler(
            database, registry, JobQueue(database), interval=timedelta(0)
        )

        scheduler.produce(NOW)

        assert _events(database) == []
    finally:
        database.engine.dispose()


def test_the_warning_fires_at_most_once_per_account_per_day(tmp_path: Path) -> None:
    """A warning younger than 24 h suppresses; an older one does not."""
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    try:
        fresh = _account(database, token_ref="ref-1", platform="tiktok", external_id="tt-a")
        stale = _account(database, token_ref="ref-2", platform="tiktok", external_id="tt-b")
        _store(registry, "ref-1", _tiktok_token(refresh_expires_at=NOW + timedelta(days=13)))
        _store(registry, "ref-2", _tiktok_token(refresh_expires_at=NOW + timedelta(days=13)))
        with database.session() as session:
            session.add_all(
                [
                    EventRecord(
                        workspace_id=1,
                        type="account.updated",
                        subject_type="account",
                        subject_id=str(fresh),
                        payload_json=json.dumps({"warning": "refresh_token_expiring"}),
                        created_at=NOW - timedelta(hours=1),
                    ),
                    EventRecord(
                        workspace_id=1,
                        type="account.updated",
                        subject_type="account",
                        subject_id=str(stale),
                        payload_json=json.dumps({"warning": "refresh_token_expiring"}),
                        created_at=NOW - timedelta(hours=25),
                    ),
                ]
            )
        scheduler = TokenUpkeepScheduler(
            database, registry, JobQueue(database), interval=timedelta(0)
        )

        scheduler.produce(NOW)

        # The account warned an hour ago stays quiet; the one warned 25 h ago
        # warns again (the client may have missed the first).
        events = _events(database)
        fresh_count = sum(1 for s, p in events if s == str(fresh) and p.get("warning"))
        stale_count = sum(1 for s, p in events if s == str(stale) and p.get("warning"))
        assert fresh_count == 1  # only the seed
        assert stale_count == 2  # seed + the repeat warning
    finally:
        database.engine.dispose()


def test_the_warning_is_per_account(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    try:
        first = _account(database, token_ref="ref-1", platform="tiktok", external_id="tt-a")
        second = _account(database, token_ref="ref-2", platform="tiktok", external_id="tt-b")
        _store(registry, "ref-1", _tiktok_token(refresh_expires_at=NOW + timedelta(days=13)))
        _store(registry, "ref-2", _tiktok_token(refresh_expires_at=NOW + timedelta(days=5)))
        scheduler = TokenUpkeepScheduler(
            database, registry, JobQueue(database), interval=timedelta(0)
        )

        scheduler.produce(NOW)

        events = dict(_events(database))
        assert events[str(first)]["days_left"] == 13
        assert events[str(second)]["days_left"] == 5
    finally:
        database.engine.dispose()


def test_other_account_events_do_not_suppress_the_warning(tmp_path: Path) -> None:
    """An unrelated account.updated event is not the refresh warning."""
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    try:
        account_id = _account(database, token_ref="ref-1", platform="tiktok")
        _store(registry, "ref-1", _tiktok_token(refresh_expires_at=NOW + timedelta(days=13)))
        with database.session() as session:
            session.add(
                EventRecord(
                    workspace_id=1,
                    type="account.updated",
                    subject_type="account",
                    subject_id=str(account_id),
                    payload_json=json.dumps({"status": "needs_reauth"}),
                )
            )
        scheduler = TokenUpkeepScheduler(
            database, registry, JobQueue(database), interval=timedelta(0)
        )

        scheduler.produce(NOW)

        warnings = [p for _s, p in _events(database) if p.get("warning")]
        assert warnings == [{"warning": "refresh_token_expiring", "days_left": 13}]
    finally:
        database.engine.dispose()
