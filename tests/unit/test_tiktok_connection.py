"""TikTok connection: incremental consent scopes, lifetimes, granted-scope persistence.

The provider requests read scopes at connect; publishing scopes are added later,
per account, when publishing is enabled. Every exchange and refresh stores the
scopes the response actually granted (spec 08 section 1).
"""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
from sqlalchemy import select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ConnectedAccount
from runway.platforms.base import TokenSet
from runway.platforms.capabilities import TIKTOK_SCOPES, Platform
from runway.platforms.registry import ProviderRegistry
from runway.platforms.tiktok import TikTokProvider


def _provider(handler, **update) -> TikTokProvider:
    settings = Settings(
        tiktok_client_key="tt-key",
        tiktok_client_secret="tt-secret",
        **update,
    )
    client = httpx.Client(transport=httpx.MockTransport(handler), timeout=20.0)
    return TikTokProvider(settings, client)


def test_authorization_url_requests_read_scopes_by_default() -> None:
    provider = _provider(lambda request: httpx.Response(500))
    url = provider.authorization_url(
        state="st",
        redirect_uri="https://edge/api/platforms/tiktok/oauth/callback",
        code_challenge=None,
    )
    scope = parse_qs(urlparse(url).query)["scope"][0].split(",")
    assert scope == list(TIKTOK_SCOPES)
    assert "video.upload" not in scope
    assert "video.publish" not in scope


def test_authorization_url_accepts_incremental_scopes() -> None:
    provider = _provider(lambda request: httpx.Response(500))
    url = provider.authorization_url(
        state="st",
        redirect_uri="https://edge/api/platforms/tiktok/oauth/callback",
        code_challenge=None,
        scopes=[*TIKTOK_SCOPES, "video.upload", "video.publish"],
    )
    scope = parse_qs(urlparse(url).query)["scope"][0].split(",")
    assert "video.upload" in scope
    assert "video.publish" in scope
    assert "user.info.basic" in scope


def test_refresh_keeps_previous_scopes_when_the_response_omits_them() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 7200,
                "refresh_expires_in": 15552000,
                "open_id": "open-1",
                # no "scope" field: the grant is unchanged
            },
        )

    provider = _provider(handler)
    token = TokenSet(
        access_token="old",
        refresh_token="old-refresh",
        scopes=[*TIKTOK_SCOPES, "video.upload"],
    )
    refreshed = provider.refresh(token)
    assert refreshed.scopes == [*TIKTOK_SCOPES, "video.upload"]
    assert refreshed.refresh_token == "new-refresh"


def test_refresh_stores_scopes_the_response_reports() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_in": 7200,
                "refresh_expires_in": 15552000,
                "open_id": "open-1",
                "scope": "user.info.basic,video.list,video.upload",
            },
        )

    provider = _provider(handler)
    refreshed = provider.refresh(TokenSet(access_token="old", refresh_token="r"))
    assert refreshed.scopes == ["user.info.basic", "video.list", "video.upload"]


def _expired(scopes: list[str]) -> TokenSet:
    return TokenSet(
        access_token="access-1",
        refresh_token="refresh-1",
        expires_at=datetime(2020, 1, 1, tzinfo=UTC),
        scopes=scopes,
    )


def _account_with_token(database: Database, registry: ProviderRegistry, token: TokenSet) -> int:
    token_ref = registry.token_store.new_token_ref()
    registry.token_store.store(token_ref, token)
    with database.session() as session:
        account = ConnectedAccount(
            platform="tiktok",
            external_account_id="open-1",
            handle="@owner",
            status="connected",
            granted_scopes_json=json.dumps(list(TIKTOK_SCOPES)),
            token_ref=token_ref,
            metadata_json="{}",
        )
        session.add(account)
        session.flush()
        return account.id


def _token_ref(database: Database, account_id: int) -> str | None:
    with database.session() as session:
        row = session.get(ConnectedAccount, account_id)
        assert row is not None
        return row.token_ref


def _granted(database: Database, account_id: int) -> list[str]:
    with database.session() as session:
        row = session.get(ConnectedAccount, account_id)
        assert row is not None
        return json.loads(row.granted_scopes_json)  # type: ignore[no-any-return]


def test_fresh_token_persists_the_scopes_a_refresh_grants(
    database: Database, settings: Settings
) -> None:
    settings = settings.model_copy(update={"tiktok_client_key": "k", "tiktok_client_secret": "s"})

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "expires_in": 7200,
                "refresh_expires_in": 15552000,
                "scope": "user.info.basic,video.list,video.upload",
            },
        )

    registry = ProviderRegistry(database, settings)
    registry.set_provider(
        Platform.tiktok,
        TikTokProvider(settings, httpx.Client(transport=httpx.MockTransport(handler))),
    )
    account_id = _account_with_token(database, registry, _expired(list(TIKTOK_SCOPES)))
    token_ref = str(_token_ref(database, account_id))
    refreshed = registry.fresh_token(Platform.tiktok, token_ref)
    assert refreshed.access_token == "access-2"
    assert _granted(database, account_id) == ["user.info.basic", "video.list", "video.upload"]


def test_concurrent_refresh_persists_the_rotated_refresh_token_once(
    database: Database, settings: Settings
) -> None:
    """Two registries racing one refresh: one HTTP call, one stored token set."""
    settings = settings.model_copy(update={"tiktok_client_key": "k", "tiktok_client_secret": "s"})
    calls = 0
    call_lock = threading.Lock()

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        with call_lock:
            calls += 1
        return httpx.Response(
            200,
            json={
                "access_token": "access-2",
                "refresh_token": "refresh-2",
                "expires_in": 7200,
                "refresh_expires_in": 15552000,
                "scope": "user.info.basic,video.list,video.upload",
            },
        )

    seed = ProviderRegistry(database, settings)
    account_id = _account_with_token(database, seed, _expired(list(TIKTOK_SCOPES)))
    token_ref = str(_token_ref(database, account_id))
    results: list[TokenSet] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def refresh() -> None:
        registry = ProviderRegistry(database, settings)
        registry.set_provider(
            Platform.tiktok,
            TikTokProvider(settings, httpx.Client(transport=httpx.MockTransport(handler))),
        )
        barrier.wait()
        try:
            results.append(registry.fresh_token(Platform.tiktok, token_ref))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=refresh) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    assert calls == 1
    stored = seed.token_store.load(token_ref)
    assert stored is not None
    assert stored.refresh_token == "refresh-2"
    assert all(token.refresh_token == "refresh-2" for token in results)
    assert _granted(database, account_id) == ["user.info.basic", "video.list", "video.upload"]


def test_realtime_seam_is_a_callable_no_op_without_a_database() -> None:
    from runway.platforms import realtime_seam

    # Wired to the realtime edge when it merges; nothing happens before that.
    assert (
        realtime_seam.on_publish_event(
            "post.publish.complete", publish_id="pub-1", account_id=1, target_id=2
        )
        is False
    )
    # `boost` asks the realtime sync to hurry; with no database wired it is a
    # no-op even for a scope the scheduler does not know.
    assert realtime_seam.boost(None, account_id=1, scope="videos", minutes=30) is None


def test_realtime_seam_boost_drives_the_sync_schedule(database: Database) -> None:
    from runway.db.models import AccountSyncState
    from runway.platforms import realtime_seam

    with database.session() as session:
        account = ConnectedAccount(
            platform="tiktok",
            external_account_id="openid-boost",
            status="connected",
            granted_scopes_json="[]",
        )
        session.add(account)
        session.flush()
        account_id = int(account.id)

    realtime_seam.boost(database, account_id, "inventory", 30)

    with database.session() as session:
        state = session.scalar(
            select(AccountSyncState).where(
                AccountSyncState.account_id == account_id,
                AccountSyncState.scope == "inventory",
            )
        )
        assert state is not None
        assert state.next_run_at is not None and state.boosted_until is not None
        # `next_run_at` was pulled to the boost moment; the window runs 30 min.
        assert state.boosted_until - state.next_run_at == timedelta(minutes=30)
        assert state.source == "push"


def test_realtime_seam_nudges_the_named_target(database: Database) -> None:
    from runway.db.models import ContentDraft, DraftTarget, PublishJob
    from runway.platforms import realtime_seam

    with database.session() as session:
        draft = ContentDraft(title="Clip", body="", status="draft")
        session.add(draft)
        session.flush()
        target = DraftTarget(
            draft_id=draft.id,
            platform="tiktok",
            account_id=None,
            status="pending",
            state="processing",
            external_id="pub-9",
        )
        session.add(target)
        session.flush()
        target_id = target.id

    assert (
        realtime_seam.on_publish_event(
            "post.publish.complete", publish_id="pub-9", database=database
        )
        is True
    )
    assert (
        realtime_seam.on_publish_event("post.publish.failed", publish_id="other", database=database)
        is False
    )
    with database.session() as session:
        jobs = session.scalars(
            select(PublishJob).where(
                PublishJob.target_id == target_id,
                PublishJob.kind == "poll_processing",
            )
        ).all()
    assert len(jobs) == 1
