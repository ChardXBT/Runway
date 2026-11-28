"""Regressions found in the 2026-11-17 readiness audit: token refresh, backfill, logs."""

from __future__ import annotations

import io
import json
import logging
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import ConnectedAccount, PlatformAction, SyncCursor
from runway.logging import configure_logging
from runway.platforms.base import (
    AuthorizationExpired,
    ContentItem,
    Page,
    ProviderError,
    ReplyResult,
    TokenSet,
)
from runway.platforms.capabilities import YOUTUBE_ENGAGE_SCOPE, Platform
from runway.platforms.inbox import InboxReplyForbidden, InboxService
from runway.platforms.mock import MockPlatformProvider
from runway.platforms.registry import (
    INSTAGRAM_TOKEN_REFRESH_WINDOW,
    TOKEN_REFRESH_WINDOW,
    ProviderRegistry,
    token_refresh_window,
)
from runway.platforms.sync import SyncService
from runway.platforms.youtube import YouTubeProvider


@pytest.fixture()
def env(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        inbox_replies_enabled=True,
        youtube_engage_scopes=True,
    )
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    yield settings, database, registry
    database.engine.dispose()


def _connected(
    database: Database, registry: ProviderRegistry, platform: str, token: TokenSet
) -> int:
    token_ref = registry.token_store.new_token_ref()
    with database.session() as session:
        account = ConnectedAccount(
            platform=platform,
            external_account_id=f"{platform}-1",
            status="connected",
            granted_scopes_json=json.dumps([YOUTUBE_ENGAGE_SCOPE]),
            token_ref=token_ref,
        )
        session.add(account)
        session.flush()
        account_id = account.id
    registry.token_store.store(token_ref, token)
    return account_id


def _expiring(**extra: Any) -> TokenSet:
    return TokenSet(
        access_token="old-access",
        refresh_token="refresh",
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
        **extra,
    )


def _status(database: Database, account_id: int) -> str:
    with database.session() as session:
        account = session.get(ConnectedAccount, account_id)
        assert account is not None
        return account.status


# ------------------------------------------------------------ refresh failures


def test_transient_refresh_failure_fails_the_run_but_keeps_the_account_connected(env) -> None:
    settings, database, registry = env

    class FlakyRefresh(MockPlatformProvider):
        def refresh(self, token: TokenSet) -> TokenSet:
            raise ProviderError("YouTube request failed: timed out", retryable=True)

    registry.set_provider(Platform.youtube, FlakyRefresh(Platform.youtube, settings))
    account_id = _connected(database, registry, "youtube", _expiring())

    run = SyncService(database, settings, registry).run(account_id)

    assert run.status == "failed"
    assert "timed out" in (run.error_summary or "")
    # Before the fix a network blip during refresh flipped the account to
    # needs_reauth, and the scheduler then never synced it again.
    assert _status(database, account_id) == "connected"


def test_revoked_grant_during_refresh_still_needs_reauth(env) -> None:
    settings, database, registry = env

    class RevokedRefresh(MockPlatformProvider):
        def refresh(self, token: TokenSet) -> TokenSet:
            raise AuthorizationExpired("invalid_grant", status=400)

    registry.set_provider(Platform.youtube, RevokedRefresh(Platform.youtube, settings))
    account_id = _connected(database, registry, "youtube", _expiring())

    run = SyncService(database, settings, registry).run(account_id)

    assert run.status == "failed"
    assert _status(database, account_id) == "needs_reauth"


def test_failed_refresh_keeps_the_original_token_and_can_retry(env) -> None:
    settings, database, registry = env
    attempts: list[str] = []

    class FlakyThenFresh(MockPlatformProvider):
        def refresh(self, token: TokenSet) -> TokenSet:
            attempts.append(token.access_token)
            if len(attempts) == 1:
                raise ProviderError("YouTube request failed: timed out", retryable=True)
            return token.model_copy(
                update={
                    "access_token": "retry-access",
                    "expires_at": datetime.now(UTC) + timedelta(hours=1),
                }
            )

    registry.set_provider(Platform.youtube, FlakyThenFresh(Platform.youtube, settings))
    token_ref = registry.token_store.new_token_ref()
    registry.token_store.store(token_ref, _expiring())

    with pytest.raises(ProviderError, match="timed out"):
        registry.fresh_token(Platform.youtube, token_ref)
    stored = registry.token_store.load(token_ref)
    assert stored is not None and stored.access_token == "old-access"

    retried = registry.fresh_token(Platform.youtube, token_ref)

    assert attempts == ["old-access", "old-access"]
    assert retried.access_token == "retry-access"
    stored = registry.token_store.load(token_ref)
    assert stored is not None and stored.access_token == "retry-access"


def test_tokens_outside_the_refresh_window_are_returned_without_provider_work(env) -> None:
    settings, database, registry = env

    class MustNotRefresh(MockPlatformProvider):
        def refresh(self, token: TokenSet) -> TokenSet:  # pragma: no cover
            raise AssertionError("fresh tokens must not call the provider")

    registry.set_provider(Platform.youtube, MustNotRefresh(Platform.youtube, settings))
    no_expiry_ref = registry.token_store.new_token_ref()
    fresh_ref = registry.token_store.new_token_ref()
    registry.token_store.store(no_expiry_ref, TokenSet(access_token="no-expiry"))
    registry.token_store.store(
        fresh_ref,
        TokenSet(access_token="fresh", expires_at=datetime.now(UTC) + timedelta(hours=1)),
    )

    assert registry.fresh_token(Platform.youtube, no_expiry_ref).access_token == "no-expiry"
    assert registry.fresh_token(Platform.youtube, fresh_ref).access_token == "fresh"


@pytest.mark.parametrize("remaining", [timedelta(0), timedelta(seconds=-1)])
def test_zero_or_negative_remaining_token_lifetime_refreshes(env, remaining: timedelta) -> None:
    settings, database, registry = env

    class RefreshingProvider(MockPlatformProvider):
        def refresh(self, token: TokenSet) -> TokenSet:
            return token.model_copy(
                update={
                    "access_token": "fresh-after-expiry",
                    "expires_at": datetime.now(UTC) + timedelta(hours=1),
                }
            )

    registry.set_provider(Platform.youtube, RefreshingProvider(Platform.youtube, settings))
    token_ref = registry.token_store.new_token_ref()
    registry.token_store.store(
        token_ref,
        TokenSet(access_token="expiring-now", expires_at=datetime.now(UTC) + remaining),
    )

    assert registry.fresh_token(Platform.youtube, token_ref).access_token == "fresh-after-expiry"


def test_naive_expiry_is_interpreted_as_utc_instead_of_crashing(env) -> None:
    settings, database, registry = env

    class RefreshingProvider(MockPlatformProvider):
        def refresh(self, token: TokenSet) -> TokenSet:
            return token.model_copy(
                update={
                    "access_token": "fresh-naive-expiry",
                    "expires_at": datetime.now(UTC) + timedelta(hours=1),
                }
            )

    registry.set_provider(Platform.youtube, RefreshingProvider(Platform.youtube, settings))
    token_ref = registry.token_store.new_token_ref()
    naive_expiry = (datetime.now(UTC) + timedelta(minutes=1)).replace(tzinfo=None)
    registry.token_store.store(
        token_ref,
        TokenSet(access_token="naive-expiry", expires_at=naive_expiry),
    )

    assert registry.fresh_token(Platform.youtube, token_ref).access_token == "fresh-naive-expiry"


def test_non_utc_expiry_is_compared_as_an_instant(env) -> None:
    settings, database, registry = env

    class MustNotRefresh(MockPlatformProvider):
        def refresh(self, token: TokenSet) -> TokenSet:  # pragma: no cover
            raise AssertionError("fresh non-UTC tokens must not call the provider")

    registry.set_provider(Platform.youtube, MustNotRefresh(Platform.youtube, settings))
    token_ref = registry.token_store.new_token_ref()
    offset = timezone(timedelta(hours=-5))
    registry.token_store.store(
        token_ref,
        TokenSet(
            access_token="offset-expiry",
            expires_at=datetime.now(offset) + timedelta(hours=1),
        ),
    )

    assert registry.fresh_token(Platform.youtube, token_ref).access_token == "offset-expiry"


def test_instagram_long_lived_tokens_refresh_well_before_expiry(env) -> None:
    settings, database, registry = env
    refreshed: list[str] = []

    class CountingRefresh(MockPlatformProvider):
        def refresh(self, token: TokenSet) -> TokenSet:
            refreshed.append(token.access_token)
            return token.model_copy(
                update={
                    "access_token": "new-access",
                    "expires_at": datetime.now(UTC) + timedelta(days=60),
                }
            )

    registry.set_provider(Platform.instagram, CountingRefresh(Platform.instagram, settings))
    assert token_refresh_window(Platform.instagram) == INSTAGRAM_TOKEN_REFRESH_WINDOW
    assert token_refresh_window(Platform.youtube) == TOKEN_REFRESH_WINDOW

    token_ref = registry.token_store.new_token_ref()
    # 20 days left of a 60-day token: a five-minute window would wait until the
    # token was nearly dead, so a sync any time now must refresh it.
    registry.token_store.store(
        token_ref,
        TokenSet(access_token="ig-old", expires_at=datetime.now(UTC) + timedelta(days=20)),
    )
    token = registry.fresh_token(Platform.instagram, token_ref)
    assert refreshed == ["ig-old"]
    assert token.access_token == "new-access"
    stored = registry.token_store.load(token_ref)
    assert stored is not None and stored.access_token == "new-access"

    # A freshly issued long-lived token is left alone.
    registry.fresh_token(Platform.instagram, token_ref)
    assert refreshed == ["ig-old"]


def test_youtube_refresh_keeps_the_channel_context_recorded_at_connect() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/token"
        return httpx.Response(200, json={"access_token": "new-access", "expires_in": 3600})

    provider = YouTubeProvider(
        Settings(youtube_client_id="id", youtube_client_secret="secret"),
        httpx.Client(transport=httpx.MockTransport(handler)),
    )
    token = _expiring(extra={"channel_id": "UCchan", "uploads_playlist": "UUuploads"})

    refreshed = provider.refresh(token)

    assert refreshed.access_token == "new-access"
    assert refreshed.refresh_token == "refresh"
    # Without channel_id every owner reply looked like a viewer comment.
    assert refreshed.extra == {"channel_id": "UCchan", "uploads_playlist": "UUuploads"}


# ------------------------------------------------------------------- inbox


def _comment(database: Database, account_id: int) -> int:
    from runway.db.models import CommentRecord, utcnow

    with database.session() as session:
        comment = CommentRecord(
            account_id=account_id,
            platform="youtube",
            external_id="c1",
            text="hello",
            status="new",
            has_owner_reply=False,
            published_at=utcnow(),
        )
        session.add(comment)
        session.flush()
        return comment.id


def test_reply_refreshes_an_expired_token_before_sending(env) -> None:
    settings, database, registry = env
    sent_with: list[str] = []

    class RefreshingProvider(MockPlatformProvider):
        def refresh(self, token: TokenSet) -> TokenSet:
            return token.model_copy(
                update={
                    "access_token": "fresh-access",
                    "expires_at": datetime.now(UTC) + timedelta(hours=1),
                }
            )

        def reply_to_comment(
            self, token: TokenSet, *, comment_external_id: str, text: str
        ) -> ReplyResult:
            sent_with.append(token.access_token)
            return ReplyResult(external_id="r1", verified=True, detail="ok")

    registry.set_provider(Platform.youtube, RefreshingProvider(Platform.youtube, settings))
    account_id = _connected(database, registry, "youtube", _expiring())
    comment_id = _comment(database, account_id)

    InboxService(database, settings, registry).reply(comment_id, "thanks!")

    assert sent_with == ["fresh-access"]


def test_reply_with_a_revoked_grant_is_forbidden_and_the_action_is_closed(env) -> None:
    settings, database, registry = env

    class RevokedProvider(MockPlatformProvider):
        def refresh(self, token: TokenSet) -> TokenSet:
            raise AuthorizationExpired("invalid_grant", status=400)

        def reply_to_comment(self, token, *, comment_external_id, text):  # pragma: no cover
            raise AssertionError("an expired token must never be sent")

    registry.set_provider(Platform.youtube, RevokedProvider(Platform.youtube, settings))
    account_id = _connected(database, registry, "youtube", _expiring())
    comment_id = _comment(database, account_id)

    with pytest.raises(InboxReplyForbidden, match="reconnect"):
        InboxService(database, settings, registry).reply(comment_id, "thanks!")
    with database.session() as session:
        action = session.scalar(select(PlatformAction))
        assert action is not None
        assert action.state == "failed"
        assert action.completed_at is not None


# ----------------------------------------------------------------- backfill


def _item(external_id: str) -> ContentItem:
    return ContentItem(external_id=external_id, content_type="video", title=external_id)


def test_empty_backfill_page_does_not_pin_the_resume_cursor(env) -> None:
    settings, database, registry = env
    pages = {
        None: Page[ContentItem](items=[_item("a")], next_cursor={"after": "A"}),
        # Instagram's last page still returns an "after" cursor; the next page
        # is then empty with no cursor.
        "A": Page[ContentItem](items=[_item("b")], next_cursor={"after": "B"}),
        "B": Page[ContentItem](items=[], next_cursor=None),
    }

    class PagedProvider(MockPlatformProvider):
        def list_content(self, token, *, cursor, limit):
            return pages[cursor["after"] if cursor else None]

    registry.set_provider(Platform.youtube, PagedProvider(Platform.youtube, settings))
    account_id = _connected(database, registry, "youtube", TokenSet(access_token="t"))
    service = SyncService(database, settings, registry)
    provider = registry.provider(Platform.youtube)

    service._step_content(provider, account_id, TokenSet(access_token="t"))

    with database.session() as session:
        cursors = [
            json.loads(row.cursor_json)
            for row in session.scalars(
                select(SyncCursor).where(SyncCursor.account_id == account_id)
            )
        ]
    # Before the fix the stored cursor stayed {"resume": {"after": "B"}} and the
    # dead page was refetched forever without the backfill completing.
    assert any(cursor.get("complete") for cursor in cursors), cursors


def test_filtered_out_page_still_advances_the_backfill(env) -> None:
    settings, database, registry = env
    pages = {
        None: Page[ContentItem](items=[_item("a")], next_cursor={"after": "A"}),
        # A page whose videos were all private/deleted comes back empty but
        # has a next page.
        "A": Page[ContentItem](items=[], next_cursor={"after": "B"}),
        "B": Page[ContentItem](items=[_item("c")], next_cursor=None),
    }

    class PagedProvider(MockPlatformProvider):
        def list_content(self, token, *, cursor, limit):
            return pages[cursor["after"] if cursor else None]

    registry.set_provider(Platform.youtube, PagedProvider(Platform.youtube, settings))
    account_id = _connected(database, registry, "youtube", TokenSet(access_token="t"))
    service = SyncService(database, settings, registry)
    provider = registry.provider(Platform.youtube)

    for _ in range(3):
        service._step_content(provider, account_id, TokenSet(access_token="t"))

    from runway.db.models import PlatformContent

    with database.session() as session:
        imported = set(
            session.scalars(
                select(PlatformContent.external_id).where(PlatformContent.account_id == account_id)
            )
        )
    assert imported == {"a", "c"}


# ------------------------------------------------------------------ logging


def test_http_request_urls_are_not_logged_at_info() -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    saved_levels = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    try:
        configure_logging()
        buffer = io.StringIO()
        root.handlers[0].setStream(buffer)  # type: ignore[attr-defined]
        secret_url = (
            "https://graph.instagram.com/access_token?grant_type=ig_exchange_token"
            "&client_secret=APP-SECRET&access_token=USER-TOKEN"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            client.get(secret_url)
        logging.getLogger("runway.test").info("still logging application events")

        output = buffer.getvalue()
        assert "APP-SECRET" not in output
        assert "USER-TOKEN" not in output
        assert "still logging application events" in output
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)
