from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select

from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import (
    AccountMetricDay,
    AccountMetricSnapshot,
    CommentRecord,
    ConnectedAccount,
    ContentMetricSnapshot,
    PlatformContent,
    SyncCursor,
    SyncRun,
)
from runway.platforms.base import (
    AuthorizationExpired,
    ProviderError,
    RateLimited,
)
from runway.platforms.capabilities import Platform
from runway.platforms.mock import MockPlatformProvider
from runway.platforms.registry import ProviderRegistry
from runway.platforms.sync import SyncRunInProgress, SyncService


@pytest.fixture()
def sync_env(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        platform_sync_max_content_items=200,
    )
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    registry.ensure_mock_accounts()
    service = SyncService(database, settings, registry)
    yield settings, database, registry, service
    database.engine.dispose()


def _account(database: Database, platform: str) -> ConnectedAccount:
    with database.session() as session:
        account = session.scalar(
            select(ConnectedAccount).where(ConnectedAccount.platform == platform)
        )
        assert account is not None
        session.expunge(account)
        return account


def _counts(database: Database, model, account_id: int | None = None) -> int:
    with database.session() as session:
        statement = select(func.count()).select_from(model)
        if account_id is not None and hasattr(model, "account_id"):
            statement = statement.where(model.account_id == account_id)
        return int(session.scalar(statement) or 0)


def test_full_run_creates_content_comments_and_metrics(sync_env) -> None:
    _settings, database, _registry, service = sync_env
    account = _account(database, "youtube")
    run = service.run(account.id, trigger="manual")
    assert run.status == "completed"
    steps = json.loads(run.steps_json)
    assert [step["step"] for step in steps] == [
        "identity",
        "content",
        "content_metrics",
        "account_daily_metrics",
        "comments",
    ]
    assert all(step["status"] == "completed" for step in steps)
    assert _counts(database, PlatformContent, account.id) == 12
    assert _counts(database, CommentRecord, account.id) > 20
    assert _counts(database, AccountMetricDay, account.id) > 0
    assert _counts(database, AccountMetricSnapshot, account.id) >= 1
    assert _counts(database, ContentMetricSnapshot) >= 1


def test_second_run_is_idempotent_and_preserves_status(sync_env) -> None:
    _settings, database, _registry, service = sync_env
    account = _account(database, "youtube")
    service.run(account.id)
    first_seen_counts = _counts(database, PlatformContent)
    with database.session() as session:
        comment = session.scalar(select(CommentRecord).limit(1))
        assert comment is not None
        comment.status = "read"
        comment_id = comment.id
        first_seen = comment.first_seen_at
    service.run(account.id)
    assert _counts(database, PlatformContent) == first_seen_counts
    with database.session() as session:
        comment = session.scalar(select(CommentRecord).where(CommentRecord.id == comment_id))
        assert comment is not None
        assert comment.status == "read"
        assert comment.first_seen_at == first_seen
        assert comment.last_seen_at >= first_seen
    # One mock_seed run from account creation plus the two manual runs.
    runs = _counts(database, SyncRun, account.id)
    assert runs == 3


def test_cursor_persisted_and_max_items_bound(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        platform_sync_max_content_items=10,
    )
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    registry.ensure_mock_accounts()
    service = SyncService(database, settings, registry)
    account = _account(database, "youtube")
    # The seed run respected the 10-item bound (head page + backfill pages);
    # the remaining items arrive on the follow-up run below.
    with database.session() as session:
        seed_count = session.scalar(
            select(func.count())
            .select_from(PlatformContent)
            .where(PlatformContent.account_id == account.id)
        )
        assert seed_count == 10
        cursor = session.scalar(
            select(SyncCursor).where(
                SyncCursor.account_id == account.id, SyncCursor.scope == "content"
            )
        )
        assert cursor is not None
        assert json.loads(cursor.cursor_json) == {"resume": {"offset": 10}}
    service.run(account.id)
    with database.session() as session:
        count = session.scalar(
            select(func.count())
            .select_from(PlatformContent)
            .where(PlatformContent.account_id == account.id)
        )
        assert count == 12
    database.engine.dispose()


def test_failing_comments_step_marks_run_partial(sync_env) -> None:
    settings, database, registry, service = sync_env

    class FailingComments(MockPlatformProvider):
        def list_comments(self, token, *, content_external_id, cursor, limit):
            raise ProviderError("provider outage", retryable=True)

    registry.set_provider(Platform.youtube, FailingComments(Platform.youtube, settings))
    account = _account(database, "youtube")
    run = service.run(account.id)
    assert run.status == "partial"
    steps = json.loads(run.steps_json)
    comments_step = next(step for step in steps if step["step"] == "comments")
    assert comments_step["status"] == "failed"
    assert "provider outage" in comments_step["error"]
    content_step = next(step for step in steps if step["step"] == "content")
    assert content_step["status"] == "completed"
    assert _counts(database, PlatformContent, account.id) == 12


def test_authorization_expired_fails_run_and_marks_account(sync_env) -> None:
    settings, database, registry, service = sync_env

    class ExpiredIdentity(MockPlatformProvider):
        def fetch_identity(self, token):
            raise AuthorizationExpired("token revoked", status=401)

    registry.set_provider(Platform.youtube, ExpiredIdentity(Platform.youtube, settings))
    account = _account(database, "youtube")
    run = service.run(account.id)
    assert run.status == "failed"
    account = _account(database, "youtube")
    assert account.status == "needs_reauth"
    assert account.last_error == "token revoked"


def test_rate_limited_step_marks_partial_and_retryable(sync_env) -> None:
    settings, database, registry, service = sync_env

    class LimitedContent(MockPlatformProvider):
        def list_content(self, token, *, cursor, limit):
            raise RateLimited("quota exceeded", status=429)

    registry.set_provider(Platform.youtube, LimitedContent(Platform.youtube, settings))
    account = _account(database, "youtube")
    run = service.run(account.id)
    assert run.status == "partial"
    steps = json.loads(run.steps_json)
    content_step = next(step for step in steps if step["step"] == "content")
    assert "retryable" in content_step["error"]
    assert run.error_summary is not None and "retryable" in run.error_summary


def test_concurrent_run_is_rejected(sync_env) -> None:
    _settings, database, _registry, service = sync_env
    account = _account(database, "youtube")
    # A recent running run in the database blocks a second run.
    with database.session() as session:
        session.add(
            SyncRun(
                account_id=account.id,
                trigger="manual",
                status="running",
                started_at=datetime.now(UTC),
            )
        )
    with pytest.raises(SyncRunInProgress):
        service.run(account.id)
    # The in-process lock also blocks even when no row exists.
    lock = service._account_lock(account.id)
    lock.acquire()
    try:
        with pytest.raises(SyncRunInProgress):
            service.run(account.id)
    finally:
        lock.release()


def test_stale_running_run_marked_failed_on_startup(sync_env) -> None:
    _settings, database, _registry, service = sync_env
    account = _account(database, "youtube")
    with database.session() as session:
        stale = SyncRun(
            account_id=account.id,
            trigger="manual",
            status="running",
            started_at=datetime.now(UTC) - timedelta(minutes=45),
        )
        session.add(stale)
        session.flush()
        stale_id = stale.id
        fresh = SyncRun(
            account_id=account.id,
            trigger="manual",
            status="running",
            started_at=datetime.now(UTC),
        )
        session.add(fresh)
        session.flush()
        fresh_id = fresh.id
    assert service.mark_stale_runs_failed() == 1
    with database.session() as session:
        stale_run = session.get(SyncRun, stale_id)
        fresh_run = session.get(SyncRun, fresh_id)
        assert stale_run is not None and stale_run.status == "failed"
        assert stale_run.error_summary == "interrupted"
        assert fresh_run is not None and fresh_run.status == "running"


def test_ensure_fresh_transport_error_fails_run_and_does_not_wedge(
    sync_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    _settings, database, registry, service = sync_env

    def _broken_load(token_ref: str):
        raise httpx.ConnectError("network unreachable")

    monkeypatch.setattr(registry.token_store, "load", _broken_load)
    account = _account(database, "youtube")
    run = service.run(account.id)
    assert run.status == "failed"
    assert run.error_summary
    # The run is finished, so the account is not wedged behind the
    # recent-running-run guard: a follow-up run can start immediately.
    assert not service._has_recent_running_run(account.id)
    second = service.run(account.id)
    assert second.status == "failed"


def test_provider_construction_failure_fails_run(sync_env, monkeypatch: pytest.MonkeyPatch) -> None:
    _settings, database, registry, service = sync_env

    def _broken_provider(platform: Platform):
        raise ValueError("provider misconfigured")

    monkeypatch.setattr(registry, "provider", _broken_provider)
    account = _account(database, "youtube")
    run = service.run(account.id)
    assert run.status == "failed"
    assert "provider misconfigured" in (run.error_summary or "")


def test_tiktok_comments_step_is_skipped_not_failed(sync_env) -> None:
    _settings, database, _registry, service = sync_env
    account = _account(database, "tiktok")
    run = service.run(account.id)
    assert run.status == "completed"
    steps = json.loads(run.steps_json)
    comments_step = next(step for step in steps if step["step"] == "comments")
    assert comments_step["status"] == "skipped"
