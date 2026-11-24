"""Data duties in the purge job (spec 04 section 3, spec 05 section 5).

Two deletions ride the hourly `purge` job:

- **YouTube's 30-day refresh-or-delete rule**: rows imported from the
  YouTube provider - platform_content, metric snapshots/days and
  comment_records - are deleted once they are 30 days old and have not
  been refreshed since. Each row kind carries the instant a sync last
  refreshed it (`last_seen_at`, `metrics_fetched_at`, `fetched_at`,
  `captured_at`), so no schema change is needed.
- **The 7-day dead-account duty**: disconnect revokes at the platform,
  then this deletes the account's stored platform data (and its token,
  through ``TokenStore.delete``) once the account has been
  ``disconnected`` or ``needs_reauth`` - the revocation state - for more
  than 7 days.

Runway-authored rows (drafts, targets, proposals) are never touched.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import (
    AccountMetricDay,
    AccountMetricSnapshot,
    CommentRecord,
    ConnectedAccount,
    ContentDraft,
    ContentMetricSnapshot,
    PlatformContent,
    PlatformSubscription,
    utcnow,
)
from runway.jobs.retention import (
    DEAD_ACCOUNT_DATA_DELAY,
    purge_dead_account_data,
    purge_youtube_api_data,
)
from runway.platforms.base import TokenSet
from runway.platforms.tokens import TokenStore

NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", platform_provider="live")


def _account(
    database: Database,
    *,
    platform: str = "youtube",
    status: str = "connected",
    token_ref: str | None = "ref-1",
    updated_at: datetime | None = None,
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
        if updated_at is not None:
            account.updated_at = updated_at
        session.add(account)
        session.flush()
        return int(account.id)


def _content(
    session: Session,
    account_id: int,
    *,
    platform: str = "youtube",
    external_id: str = "vid-1",
    last_seen_at: datetime,
    metrics_fetched_at: datetime | None = None,
) -> int:
    row = PlatformContent(
        account_id=account_id,
        platform=platform,
        external_id=external_id,
        content_type="video",
        last_seen_at=last_seen_at,
        metrics_fetched_at=metrics_fetched_at,
    )
    session.add(row)
    session.flush()
    return int(row.id)


def _comment(
    session: Session,
    account_id: int,
    *,
    platform: str = "youtube",
    external_id: str = "c-1",
    content_id: int | None = None,
    last_seen_at: datetime,
) -> int:
    row = CommentRecord(
        account_id=account_id,
        platform=platform,
        external_id=external_id,
        content_id=content_id,
        text="hi",
        last_seen_at=last_seen_at,
    )
    session.add(row)
    session.flush()
    return int(row.id)


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


# ----------------------------------------------------- 30-day YouTube rule


def test_unrefreshed_youtube_rows_older_than_30_days_are_deleted(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    try:
        account_id = _account(database)
        with database.session() as session:
            stale_content = _content(
                session,
                account_id,
                external_id="stale",
                last_seen_at=NOW - timedelta(days=40),
                metrics_fetched_at=NOW - timedelta(days=40),
            )
            fresh_content = _content(
                session,
                account_id,
                external_id="fresh",
                last_seen_at=NOW - timedelta(days=10),
            )
            _comment(
                session,
                account_id,
                external_id="stale-comment",
                content_id=stale_content,
                last_seen_at=NOW - timedelta(days=40),
            )
            _comment(
                session,
                account_id,
                external_id="fresh-comment",
                content_id=fresh_content,
                last_seen_at=NOW - timedelta(days=10),
            )
            session.add(
                ContentMetricSnapshot(
                    content_id=stale_content,
                    captured_at=NOW - timedelta(days=40),
                )
            )
            session.add(
                ContentMetricSnapshot(
                    content_id=fresh_content,
                    captured_at=NOW - timedelta(days=10),
                )
            )
            session.add(
                AccountMetricDay(
                    account_id=account_id,
                    day=(NOW - timedelta(days=40)).date(),
                    metric="views",
                    value=1.0,
                    source="youtube",
                    fetched_at=NOW - timedelta(days=40),
                )
            )
            session.add(
                AccountMetricDay(
                    account_id=account_id,
                    day=NOW.date(),
                    metric="views",
                    value=2.0,
                    source="youtube",
                    fetched_at=NOW - timedelta(days=10),
                )
            )
            session.add(
                AccountMetricSnapshot(account_id=account_id, captured_at=NOW - timedelta(days=40))
            )
            session.add(
                AccountMetricSnapshot(account_id=account_id, captured_at=NOW - timedelta(days=10))
            )
            session.flush()

            removed = purge_youtube_api_data(session, now=NOW)

        with database.session() as session:
            assert _count(session, PlatformContent) == 1
            assert _count(session, CommentRecord) == 1
            assert _count(session, ContentMetricSnapshot) == 1
            assert _count(session, AccountMetricDay) == 1
            assert _count(session, AccountMetricSnapshot) == 1
        assert removed == 5
    finally:
        database.engine.dispose()


def test_a_metrics_refresh_alone_keeps_the_content_row(tmp_path: Path) -> None:
    """last_seen_at stale but metrics_fetched_at fresh: still refreshed data."""
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    try:
        account_id = _account(database)
        with database.session() as session:
            _content(
                session,
                account_id,
                last_seen_at=NOW - timedelta(days=40),
                metrics_fetched_at=NOW - timedelta(days=10),
            )

            removed = purge_youtube_api_data(session, now=NOW)

            assert _count(session, PlatformContent) == 1
            assert removed == 0
    finally:
        database.engine.dispose()


def test_other_platforms_rows_are_not_under_the_youtube_rule(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    try:
        account_id = _account(database, platform="instagram")
        with database.session() as session:
            _content(
                session,
                account_id,
                platform="instagram",
                last_seen_at=NOW - timedelta(days=400),
                metrics_fetched_at=NOW - timedelta(days=400),
            )
            _comment(
                session,
                account_id,
                platform="instagram",
                last_seen_at=NOW - timedelta(days=400),
            )

            removed = purge_youtube_api_data(session, now=NOW)

            assert _count(session, PlatformContent) == 1
            assert _count(session, CommentRecord) == 1
            assert removed == 0
    finally:
        database.engine.dispose()


def test_runway_authored_rows_survive_the_youtube_purge(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    try:
        account_id = _account(database)
        with database.session() as session:
            _content(
                session,
                account_id,
                last_seen_at=NOW - timedelta(days=40),
                metrics_fetched_at=NOW - timedelta(days=40),
            )
            session.add(ContentDraft(title="my draft", body="text"))
            session.flush()

            purge_youtube_api_data(session, now=NOW)

            assert _count(session, PlatformContent) == 0
            assert _count(session, ContentDraft) == 1
    finally:
        database.engine.dispose()


# --------------------------------------------------------- 7-day dead rule


def test_a_disconnected_accounts_platform_data_dies_after_7_days(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    try:
        dead = _account(
            database,
            status="disconnected",
            token_ref=None,
            updated_at=NOW - DEAD_ACCOUNT_DATA_DELAY - timedelta(days=1),
        )
        live = _account(database, external_id="live-1", token_ref="ref-2")
        with database.session() as session:
            dead_content = _content(session, dead, last_seen_at=NOW - timedelta(days=1))
            _content(session, live, external_id="live-vid", last_seen_at=NOW)
            session.add(ContentMetricSnapshot(content_id=dead_content, captured_at=NOW))
            _comment(
                session,
                dead,
                external_id="dead-comment",
                last_seen_at=NOW - timedelta(days=1),
            )
            session.add(
                AccountMetricDay(
                    account_id=dead,
                    day=NOW.date(),
                    metric="views",
                    value=1.0,
                    source="youtube",
                    fetched_at=NOW,
                )
            )
            session.add(AccountMetricSnapshot(account_id=dead, captured_at=NOW))
            session.add(
                PlatformSubscription(
                    account_id=dead,
                    provider="youtube",
                    topic="t",
                    state="unsubscribed",
                )
            )
            session.flush()

            removed = purge_dead_account_data(
                session,
                TokenStore(settings.resolved_data_dir / "secrets"),
                now=NOW,
            )

        with database.session() as session:
            # Only the live account's content is left; every dead row is gone.
            assert _count(session, PlatformContent) == 1
            assert _count(session, CommentRecord) == 0
            assert _count(session, ContentMetricSnapshot) == 0
            assert _count(session, AccountMetricDay) == 0
            assert _count(session, AccountMetricSnapshot) == 0
            assert _count(session, PlatformSubscription) == 0
        assert removed == 6
    finally:
        database.engine.dispose()


def test_a_recent_disconnect_keeps_its_data(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    try:
        dead = _account(
            database,
            status="disconnected",
            token_ref=None,
            updated_at=NOW - timedelta(days=2),
        )
        with database.session() as session:
            _content(session, dead, last_seen_at=NOW - timedelta(days=100))

            removed = purge_dead_account_data(
                session,
                TokenStore(settings.resolved_data_dir / "secrets"),
                now=NOW,
            )

            assert _count(session, PlatformContent) == 1
            assert removed == 0
    finally:
        database.engine.dispose()


def test_a_revoked_account_gets_the_same_deletion_path(tmp_path: Path) -> None:
    """needs_reauth is what platform-side revocation becomes (spec 05 §5)."""
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    token_store = TokenStore(settings.resolved_data_dir / "secrets")
    try:
        dead = _account(
            database,
            status="needs_reauth",
            token_ref="dead-ref",
            updated_at=NOW - timedelta(days=9),
        )
        token_store.store("dead-ref", TokenSet(access_token="dead"))
        with database.session() as session:
            _content(session, dead, last_seen_at=NOW)

            removed = purge_dead_account_data(session, token_store, now=NOW)

        assert token_store.load("dead-ref") is None
        with database.session() as session:
            assert _count(session, PlatformContent) == 0
        assert removed == 1
    finally:
        database.engine.dispose()


def test_runway_drafts_survive_the_dead_account_purge(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    try:
        dead = _account(
            database,
            status="disconnected",
            token_ref=None,
            updated_at=NOW - timedelta(days=9),
        )
        with database.session() as session:
            _content(session, dead, last_seen_at=NOW)
            session.add(ContentDraft(title="keep me", body="draft"))
            session.flush()

            purge_dead_account_data(
                session,
                TokenStore(settings.resolved_data_dir / "secrets"),
                now=NOW,
            )

            assert _count(session, PlatformContent) == 0
            assert _count(session, ContentDraft) == 1
    finally:
        database.engine.dispose()


def test_the_purge_job_runs_both_duties(tmp_path: Path) -> None:
    """run_purge_job wires the duties in; call it end to end."""
    import json as json_module

    from runway.db.models import PublishJob
    from runway.jobs.queue import Job
    from runway.jobs.retention import run_purge_job

    settings = _settings(tmp_path)
    database = initialize_database(settings)
    try:
        dead = _account(
            database,
            status="disconnected",
            token_ref=None,
            updated_at=utcnow() - timedelta(days=9),
        )
        with database.session() as session:
            _content(session, dead, last_seen_at=utcnow() - timedelta(days=1))
        job = Job.of(
            PublishJob(
                id=1,
                kind="purge",
                workspace_id=1,
                target_id=None,
                state="running",
                attempts=1,
                max_attempts=1,
                run_at=utcnow(),
                idempotency_key="test:purge",
                payload_json=json_module.dumps({}),
                last_error=None,
            )
        )

        run_purge_job(database, settings, job)

        with database.session() as session:
            assert _count(session, PlatformContent) == 0
    finally:
        database.engine.dispose()
