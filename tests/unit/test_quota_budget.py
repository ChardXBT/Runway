"""Provider quota budgets (spec 04 section 3, RT2 part C).

YouTube's 10,000 daily units are shared by the whole project and reset at
midnight Pacific. The scheduler reserves a share for publishing and replies,
divides the rest across the active YouTube accounts, and defers an account's
sync scopes to the next Pacific day once its share is spent - publishing is
never blocked. Instagram defers an hour when its usage headers pass 80%.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import (
    AccountSyncState,
    ConnectedAccount,
    EventRecord,
    PublishJob,
    QuotaUsage,
    SyncRun,
    Workspace,
    utcnow,
)
from runway.jobs.queue import JobQueue
from runway.platforms.base import AccountIdentity, Page, TokenSet
from runway.platforms.capabilities import Platform
from runway.platforms.instagram import InstagramProvider
from runway.platforms.registry import ProviderRegistry
from runway.platforms.youtube import YouTubeProvider
from runway.realtime.quota import (
    INSTAGRAM_USAGE_DEFER,
    QUOTA_BUDGET_ERROR,
    YOUTUBE_METHOD_COSTS,
    QuotaRecorder,
    call_allowed,
    enforce_instagram_usage,
    enforce_youtube_budget,
    next_pacific_reset,
    pacific_day,
    record_units,
    record_youtube_call,
    sync_budget_units,
    used_units,
    youtube_account_share,
)
from runway.realtime.schedule import SCOPES, AdaptiveSyncScheduler

# Noon Pacific on 2026-11-23 (PST, UTC-8); the quota resets at 2026-11-24T08:00Z.
START = datetime(2026, 11, 23, 20, 0, tzinfo=UTC)
RESET = datetime(2026, 11, 24, 8, 0, tzinfo=UTC)


def _aware(moment: datetime) -> datetime:
    return moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)


def _account(
    database: Database,
    platform: str = "youtube",
    status: str = "connected",
    external: str = "ext-1",
) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform=platform,
            external_account_id=external,
            status=status,
            granted_scopes_json="[]",
        )
        session.add(account)
        session.flush()
        return int(account.id)


def _active_workspace(database: Database, seen: datetime = START) -> None:
    with database.session() as session:
        workspace = session.get(Workspace, 1)
        assert workspace is not None
        workspace.last_seen_at = seen


def _states(database: Database, account_id: int) -> dict[str, AccountSyncState]:
    with database.session() as session:
        rows = session.scalars(
            select(AccountSyncState).where(AccountSyncState.account_id == account_id)
        ).all()
        for row in rows:
            session.expunge(row)
        return {row.scope: row for row in rows}


def _quota_events(database: Database, account_id: int) -> list[EventRecord]:
    with database.session() as session:
        rows = session.scalars(
            select(EventRecord).where(
                EventRecord.type == "quota.low",
                EventRecord.subject_id == str(account_id),
            )
        ).all()
        for row in rows:
            session.expunge(row)
        return list(rows)


def _quota_row(database: Database, provider: str, bucket: str, day: date) -> QuotaUsage | None:
    with database.session() as session:
        row = session.scalar(
            select(QuotaUsage).where(
                QuotaUsage.provider == provider,
                QuotaUsage.bucket == bucket,
                QuotaUsage.day == day,
            )
        )
        if row is not None:
            session.expunge(row)
        return row


def _spend(database: Database, account_id: int, units: int, *, started: datetime = START) -> None:
    """Seed the account's sync spend for the Pacific day containing `started`."""
    with database.session() as session:
        session.add(
            SyncRun(
                account_id=account_id,
                trigger="scheduled",
                status="completed",
                started_at=started,
                completed_at=started,
                quota_units_estimate=units,
            )
        )


# ------------------------------------------------------------ the cost table


def test_method_costs_match_the_rechecked_2026_table() -> None:
    assert YOUTUBE_METHOD_COSTS["videos.list"] == ("default", 1)
    assert YOUTUBE_METHOD_COSTS["channels.list"] == ("default", 1)
    assert YOUTUBE_METHOD_COSTS["playlistItems.list"] == ("default", 1)
    assert YOUTUBE_METHOD_COSTS["commentThreads.list"] == ("default", 1)
    assert YOUTUBE_METHOD_COSTS["comments.insert"] == ("default", 50)
    assert YOUTUBE_METHOD_COSTS["videos.insert"] == ("videos.insert", 100)
    assert YOUTUBE_METHOD_COSTS["thumbnails.set"] == ("default", 50)
    assert YOUTUBE_METHOD_COSTS["search.list"] == ("search.list", 100)
    assert YOUTUBE_METHOD_COSTS["videos.batchGetStats"] == ("videos.batchGetStats", 1)


def test_pacific_day_boundaries_and_next_reset() -> None:
    # 06:00 UTC on the 23rd is still the 22nd in Los Angeles (UTC-8).
    assert pacific_day(datetime(2026, 11, 23, 6, 0, tzinfo=UTC)) == date(2026, 11, 22)
    assert pacific_day(datetime(2026, 11, 23, 8, 0, tzinfo=UTC)) == date(2026, 11, 23)
    assert pacific_day(START) == date(2026, 11, 23)
    assert next_pacific_reset(START) == RESET
    # A moment after the reset belongs to the new day.
    assert next_pacific_reset(RESET) == RESET + timedelta(days=1)


# ------------------------------------------------------------- unit recording


def test_recording_calls_upserts_units_per_bucket_and_pacific_day(
    database: Database,
) -> None:
    with database.session() as session:
        assert record_youtube_call(session, "videos.list", now=START) == ("default", 1)
        record_youtube_call(session, "videos.list", now=START)
        record_youtube_call(session, "comments.insert", now=START)
        record_youtube_call(session, "search.list", now=START)
        record_youtube_call(session, "videos.batchGetStats", now=START)
        # The same call an instant before Pacific midnight lands on the prior day.
        record_youtube_call(session, "videos.list", now=RESET - timedelta(seconds=1))
        record_youtube_call(session, "videos.list", now=RESET)

    assert used_units(database, "youtube", "default", day=date(2026, 11, 23)) == 53
    assert used_units(database, "youtube", "search.list", day=date(2026, 11, 23)) == 100
    assert used_units(database, "youtube", "videos.batchGetStats", day=date(2026, 11, 23)) == 1
    # The call made after Pacific midnight opened a new day's row.
    assert used_units(database, "youtube", "default", day=date(2026, 11, 24)) == 1


def test_the_youtube_provider_records_every_api_call(tmp_path: Path) -> None:
    recorded: list[tuple[str, str, int]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "reply-1"})
        if "/commentThreads" in request.url.path:
            return httpx.Response(200, json={"items": []})
        if "/comments" in request.url.path:
            return httpx.Response(200, json={"items": [{"id": "reply-1"}]})
        if "/videos" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "v1",
                            "statistics": {
                                "viewCount": "1",
                                "likeCount": "2",
                                "commentCount": "3",
                            },
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "UC-test",
                        "snippet": {"title": "T", "customUrl": "@t"},
                        "statistics": {},
                        "contentDetails": {"relatedPlaylists": {"uploads": "UU-test"}},
                    }
                ]
            },
        )

    provider = YouTubeProvider(
        Settings(data_dir=tmp_path / "data", inbox_replies_enabled=True),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        quota_recorder=lambda method, bucket, units: recorded.append((method, bucket, units)),
    )
    token = TokenSet(access_token="token")
    provider.fetch_identity(token)
    provider.fetch_content_metrics(token, ["v1"])
    provider.list_comments(token, content_external_id="v1", cursor=None, limit=10)
    provider.reply_to_comment(token, comment_external_id="c1", text="hi")

    methods = [method for method, _bucket, _units in recorded]
    assert methods == [
        "channels.list",
        "videos.list",
        "commentThreads.list",
        "comments.insert",
        "comments.list",
    ]
    assert [method for method, _b, _u in provider.quota_calls] == methods


def test_the_quota_recorder_persists_rows(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    database = initialize_database(settings)
    try:
        recorder = QuotaRecorder(database)
        recorder("videos.list", "default", 1)
        recorder("comments.insert", "default", 50)
        today = pacific_day(datetime.now(UTC))
        assert used_units(database, "youtube", "default", day=today) == 51
    finally:
        database.engine.dispose()


def test_two_sessions_recording_the_same_key_both_succeed_and_the_units_add_up(
    database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The (provider, bucket, day) row is an atomic insert-or-add.

    A second session whose read raced the first commit believes no row
    exists. Read-then-write inserted a duplicate and died on
    uq_quota_usage_day; the upsert adds onto the committed row instead.
    """
    day = pacific_day(START)
    first = database.session_factory()
    try:
        record_units(first, "youtube", "default", 5, day=day)
        first.commit()
    finally:
        first.close()
    second = database.session_factory()
    try:
        # The stale read: the second session selected before the first
        # commit landed, so it believes the key is free.
        monkeypatch.setattr(second, "scalar", lambda *args, **kwargs: None)
        record_units(second, "youtube", "default", 3, day=day)
        second.commit()
    finally:
        second.close()
    assert used_units(database, "youtube", "default", day=day) == 8


def test_a_recording_failure_is_logged_at_warning_and_never_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Bookkeeping must not fail the platform call it rode in on."""
    settings = Settings(data_dir=tmp_path / "data")
    database = initialize_database(settings)
    try:
        recorder = QuotaRecorder(database)

        def broken_session() -> None:
            raise RuntimeError("ledger down")

        monkeypatch.setattr(database, "session", broken_session)
        # caplog's handler lives on the root logger, which an earlier
        # configure_logging() call may have cleared; attach it to the
        # module logger so this test does not depend on run order.
        quota_logger = logging.getLogger("runway.realtime.quota")
        quota_logger.addHandler(caplog.handler)
        try:
            with caplog.at_level(logging.WARNING, logger="runway.realtime.quota"):
                recorder("videos.list", "default", 1)
        finally:
            quota_logger.removeHandler(caplog.handler)
        assert "quota" in caplog.text.lower()
    finally:
        database.engine.dispose()


def test_a_failing_quota_recorder_does_not_fail_a_posted_reply(tmp_path: Path) -> None:
    """The reply is already public once the POST returns; a ledger error
    after that must not make it look failed (the caller would retry and
    post a duplicate)."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(200, json={"id": "reply-1"})
        return httpx.Response(200, json={"items": [{"id": "reply-1"}]})

    def broken(method: str, bucket: str, units: int) -> None:
        raise RuntimeError("ledger down")

    provider = YouTubeProvider(
        Settings(data_dir=tmp_path / "data", inbox_replies_enabled=True),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        quota_recorder=broken,
    )
    result = provider.reply_to_comment(
        TokenSet(access_token="token"), comment_external_id="c1", text="hi"
    )
    assert result.external_id == "reply-1"
    # The in-memory record still charged both calls the provider made.
    assert provider.quota_calls == [
        ("comments.insert", "default", 50),
        ("comments.list", "default", 1),
    ]


def test_the_registry_attaches_a_quota_recorder(settings: Settings, database: Database) -> None:
    registry = ProviderRegistry(database, settings)
    provider = registry.provider(Platform.youtube)
    assert isinstance(provider, YouTubeProvider)
    assert provider.quota_recorder is not None


# ---------------------------------------------------------------- the budget


def test_the_publish_reserve_carves_the_sync_budget(settings: Settings) -> None:
    assert sync_budget_units(settings) == 7000
    zero = Settings(data_dir=settings.data_dir, youtube_quota_publish_reserve_percent=100)
    assert sync_budget_units(zero) == 0


def test_an_accounts_share_is_the_budget_divided_across_youtube_accounts(
    settings: Settings, database: Database
) -> None:
    _account(database, external="yt-1")
    with database.session() as session:
        assert youtube_account_share(session, settings) == 7000.0
    _account(database, external="yt-2")
    with database.session() as session:
        assert youtube_account_share(session, settings) == 3500.0
    # A disconnected account no longer counts.
    _account(database, external="yt-3", status="disconnected")
    with database.session() as session:
        assert youtube_account_share(session, settings) == 3500.0


def test_zero_budget_defers_every_scope_but_never_blocks_publishing(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        youtube_quota_publish_reserve_percent=100,
    )
    database = initialize_database(settings)
    try:
        account_id = _account(database)
        _active_workspace(database)

        # A publish-category call is never blocked, even with nothing left.
        assert call_allowed(
            database, settings, provider="youtube", purpose="publish", account_id=account_id
        )
        assert not call_allowed(
            database, settings, provider="youtube", purpose="sync", account_id=account_id
        )

        assert enforce_youtube_budget(database, account_id, settings, now=START)
        states = _states(database, account_id)
        assert set(states) == set(SCOPES)
        for state in states.values():
            assert _aware(state.next_run_at) == RESET, state.scope
            assert state.last_error == QUOTA_BUDGET_ERROR, state.scope

        events = _quota_events(database, account_id)
        assert len(events) == 1

        # Enforcing again stays deferred and does not duplicate the event.
        assert enforce_youtube_budget(
            database, account_id, settings, now=START + timedelta(hours=1)
        )
        assert len(_quota_events(database, account_id)) == 1
    finally:
        database.engine.dispose()


def test_a_spent_share_defers_and_an_unspent_one_does_not(
    settings: Settings, database: Database
) -> None:
    account_id = _account(database)
    _active_workspace(database)

    # One connected YouTube account: share = 7000 units.
    assert not enforce_youtube_budget(database, account_id, settings, now=START)
    assert _quota_events(database, account_id) == []

    _spend(database, account_id, 6999)
    assert not enforce_youtube_budget(database, account_id, settings, now=START)

    _spend(database, account_id, 1)
    assert enforce_youtube_budget(database, account_id, settings, now=START)
    states = _states(database, account_id)
    assert all(_aware(state.next_run_at) == RESET for state in states.values())
    assert len(_quota_events(database, account_id)) == 1


def test_spend_from_a_previous_pacific_day_does_not_count(
    settings: Settings, database: Database
) -> None:
    account_id = _account(database)
    _active_workspace(database)
    _spend(database, account_id, 9000, started=START - timedelta(days=2))
    assert not enforce_youtube_budget(database, account_id, settings, now=START)


def test_produce_skips_a_deferred_account_and_still_serves_the_rest(
    tmp_path: Path,
) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        youtube_quota_publish_reserve_percent=100,
    )
    database = initialize_database(settings)
    try:
        youtube_id = _account(database, platform="youtube", external="yt-1")
        tiktok_id = _account(database, platform="tiktok", external="tt-1")
        _active_workspace(database)
        scheduler = AdaptiveSyncScheduler(database, JobQueue(database), settings)

        scheduler.produce(START)

        with database.session() as session:
            jobs = session.scalars(select(PublishJob)).all()
            payloads = [json.loads(job.payload_json) for job in jobs]
        enqueued_accounts = {
            payload["account_id"] for payload in payloads if "account_id" in payload
        }
        assert youtube_id not in enqueued_accounts
        assert tiktok_id in enqueued_accounts

        for state in _states(database, youtube_id).values():
            assert _aware(state.next_run_at) == RESET
            assert state.last_error == QUOTA_BUDGET_ERROR
        # A second produce does not emit a second quota.low.
        scheduler.produce(START + timedelta(minutes=1))
        assert len(_quota_events(database, youtube_id)) == 1
    finally:
        database.engine.dispose()


# --------------------------------------------------------------- instagram


def test_instagram_usage_headers_defer_every_scope_for_one_hour(
    settings: Settings, database: Database
) -> None:
    provider = InstagramProvider(settings)
    response = httpx.Response(
        200,
        json={"data": []},
        headers={
            "x-app-usage": '{"call_count":85,"total_cputime":20,"total_time":30}',
        },
    )
    provider._json_or_raise(response)
    assert provider.usage["x-app-usage"] == 85.0

    account_id = _account(database, platform="instagram")
    assert enforce_instagram_usage(database, account_id, provider.usage, now=START)
    states = _states(database, account_id)
    assert set(states) == set(SCOPES)
    for state in states.values():
        assert _aware(state.next_run_at) == START + INSTAGRAM_USAGE_DEFER, state.scope


def test_instagram_business_use_case_usage_is_parsed_nested(
    settings: Settings, database: Database
) -> None:
    provider = InstagramProvider(settings)
    response = httpx.Response(
        200,
        json={"data": []},
        headers={
            "x-business-use-case-usage": (
                '{"17841400000000000":[{"type":"IG API","call_count":81,'
                '"total_cputime":10,"total_time":12}]}'
            ),
        },
    )
    provider._json_or_raise(response)
    assert provider.usage["x-business-use-case-usage"] == 81.0

    account_id = _account(database, platform="instagram")
    assert enforce_instagram_usage(database, account_id, provider.usage, now=START)


def test_instagram_usage_below_eighty_does_not_defer(
    settings: Settings, database: Database
) -> None:
    provider = InstagramProvider(settings)
    provider._json_or_raise(
        httpx.Response(200, json={"data": []}, headers={"x-app-usage": '{"call_count":79}'})
    )
    assert provider.usage["x-app-usage"] == 79.0
    account_id = _account(database, platform="instagram")
    assert not enforce_instagram_usage(database, account_id, provider.usage, now=START)


def test_instagram_usage_backpressure_inside_a_sync(tmp_path: Path) -> None:
    """A hot usage header mid-sync defers the account's other scopes."""
    settings = Settings(data_dir=tmp_path / "data", platform_provider="mock")
    database = initialize_database(settings)
    try:
        registry = ProviderRegistry(database, settings)
        registry.ensure_mock_accounts()
        with database.session() as session:
            account = session.scalar(
                select(ConnectedAccount).where(ConnectedAccount.platform == "instagram")
            )
            assert account is not None
            account_id = int(account.id)
        _active_workspace(database)

        class HotInstagramProvider(InstagramProvider):
            def __init__(self, settings: Settings) -> None:
                super().__init__(settings)
                self.usage["x-app-usage"] = 90.0

            def fetch_identity(self, token: TokenSet) -> AccountIdentity:
                return AccountIdentity(external_account_id="ig-1", handle="ig")

            def list_comments(self, token, *, content_external_id, cursor, limit):  # type: ignore[no-untyped-def]
                return Page(items=[], next_cursor=None)

        registry.set_provider(Platform.instagram, HotInstagramProvider(settings))
        from runway.platforms.sync import SyncService

        # Make every scope due right now so only a real defer pushes them out.
        with database.session() as session:
            for state in session.scalars(select(AccountSyncState)).all():
                state.next_run_at = utcnow()

        service = SyncService(database, settings, registry)
        service.run(account_id, scopes=["comments"])

        states = _states(database, account_id)
        # `comments` ran (recorded +15 min); the >= 80% header then deferred
        # every scope to at least an hour out.
        floor = utcnow() + timedelta(minutes=59)
        for scope, state in states.items():
            assert _aware(state.next_run_at) >= floor, scope
    finally:
        database.engine.dispose()
