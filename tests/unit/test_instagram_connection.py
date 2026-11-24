"""Instagram connection: incremental consent, granted scopes, token upkeep (spec 08 §3).

`POST /api/v1/accounts/{id}/publishing/enable` issues an Instagram Login OAuth
URL that adds `instagram_business_content_publish` to the base scopes; the
callback then stores the granted scopes on the account. Unified Login scope
names (`instagram_content_publish` etc.) count as the same permissions.

The `refresh_token` job refreshes long-lived Instagram tokens once they are old
enough for Meta's `ig_refresh_token` (never before 24 h) and close enough to
expiry; a revoked token marks the account `needs_reauth`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from runway.api.app import create_app
from runway.api.platform_routes import build_platform_router
from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import ConnectedAccount, OAuthState, PublishJob
from runway.jobs.queue import JobQueue
from runway.platforms.base import (
    AccountIdentity,
    AuthorizationExpired,
    TokenSet,
)
from runway.platforms.capabilities import INSTAGRAM_SCOPES, Platform
from runway.platforms.instagram import InstagramProvider
from runway.platforms.oauth import OAuthStateService
from runway.platforms.publish_capability import INSTAGRAM_PUBLISH_SCOPE
from runway.platforms.registry import ProviderRegistry
from runway.platforms.tokens import FileTokenStore
from runway.realtime.token_upkeep import (
    REFRESH_TOKEN_JOB,
    TokenUpkeepScheduler,
    run_refresh_token_job,
)


def migrated(settings: Settings) -> Settings:
    initialize_database(settings).engine.dispose()
    return settings


def _instagram_settings(tmp_path: Path) -> Settings:
    return migrated(
        Settings(
            data_dir=tmp_path / "data",
            instagram_app_id="ig-app",
            instagram_app_secret="ig-secret",
        )
    )


def _account(
    database: Database,
    *,
    scopes: list[str],
    platform: str = "instagram",
    external_id: str = "17841400923817",
) -> ConnectedAccount:
    with database.session() as session:
        account = ConnectedAccount(
            platform=platform,
            external_account_id=external_id,
            handle="northwindstudio",
            status="connected",
            granted_scopes_json=json.dumps(scopes),
        )
        session.add(account)
        session.flush()
        session.expunge(account)
        return account


# ---------------------------------------------------------------- enable


def test_enable_issues_a_url_requesting_the_publish_scope(tmp_path: Path) -> None:
    settings = _instagram_settings(tmp_path)
    database = Database(settings)
    account = _account(database, scopes=list(INSTAGRAM_SCOPES))
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        response = client.post(f"/api/v1/accounts/{account.id}/publishing/enable")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "authorization_required"
    assert payload["requested_scopes"] == list(INSTAGRAM_SCOPES) + [INSTAGRAM_PUBLISH_SCOPE]
    query = parse_qs(urlparse(payload["authorization_url"]).query)
    assert query["scope"] == [",".join(INSTAGRAM_SCOPES + (INSTAGRAM_PUBLISH_SCOPE,))]
    state = payload["state"]
    assert state
    # The state carries the requested scopes so the callback can store them.
    with database.session() as session:
        record = session.scalar(select(OAuthState).where(OAuthState.state == state))
        assert record is not None
        meta = json.loads(record.code_verifier or "{}")
        assert meta["scopes"] == payload["requested_scopes"]


def test_enable_reports_already_enabled(tmp_path: Path) -> None:
    settings = _instagram_settings(tmp_path)
    database = Database(settings)
    account = _account(database, scopes=list(INSTAGRAM_SCOPES) + [INSTAGRAM_PUBLISH_SCOPE])
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        response = client.post(f"/api/v1/accounts/{account.id}/publishing/enable")
    assert response.status_code == 200
    assert response.json()["status"] == "already_enabled"


def test_enable_refuses_missing_accounts_and_dispatches_per_platform(
    tmp_path: Path,
) -> None:
    settings = _instagram_settings(tmp_path)
    database = Database(settings)
    youtube = _account(database, scopes=[], platform="youtube", external_id="chan1")
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        missing = client.post("/api/v1/accounts/999/publishing/enable")
        assert missing.status_code == 404
        assert missing.headers["content-type"].startswith("application/problem+json")
        # YouTube has its own branch now: this account reaches it and fails
        # the "OAuth configured" gate, not an "unsupported platform" one.
        other = client.post(f"/api/v1/accounts/{youtube.id}/publishing/enable")
        assert other.status_code == 409
        assert other.json()["code"] == "capability_not_ready"


def test_enable_is_rejected_in_mock_mode(tmp_path: Path) -> None:
    settings = migrated(Settings(data_dir=tmp_path / "data", platform_provider="mock"))
    database = Database(settings)
    account = _account(database, scopes=list(INSTAGRAM_SCOPES))
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        response = client.post(f"/api/v1/accounts/{account.id}/publishing/enable")
    assert response.status_code == 409
    assert response.json()["code"] == "capability_not_ready"


def test_enable_requires_instagram_oauth_configuration(tmp_path: Path) -> None:
    settings = migrated(Settings(data_dir=tmp_path / "data"))
    database = Database(settings)
    account = _account(database, scopes=list(INSTAGRAM_SCOPES))
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        response = client.post(f"/api/v1/accounts/{account.id}/publishing/enable")
    assert response.status_code == 409
    assert response.json()["code"] == "capability_not_ready"


# ------------------------------------------------------- callback storage


class _FakeInstagramProvider(InstagramProvider):
    """exchange/fetch without a network: returns the granted scopes like Meta does."""

    def __init__(self, settings: Settings, scopes: list[str]) -> None:
        super().__init__(settings)
        self._scopes = scopes
        self.requested_scopes: list[str] | None = None

    def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None,
        requested_scopes: list[str] | None = None,
    ) -> TokenSet:
        self.requested_scopes = requested_scopes
        return TokenSet(access_token="long", scopes=list(self._scopes))

    def fetch_identity(self, token: TokenSet) -> AccountIdentity:
        return AccountIdentity(external_account_id="17841400923817", handle="northwindstudio")


def test_callback_after_enable_stores_the_granted_scopes(tmp_path: Path) -> None:
    settings = _instagram_settings(tmp_path)
    database = Database(settings)
    account = _account(database, scopes=list(INSTAGRAM_SCOPES))
    granted = list(INSTAGRAM_SCOPES) + [INSTAGRAM_PUBLISH_SCOPE]
    oauth_states = OAuthStateService(database)
    issued = oauth_states.issue(
        Platform.instagram,
        redirect_uri="http://127.0.0.1:8000/api/platforms/instagram/oauth/callback",
        use_pkce=False,
        payload=json.dumps({"scopes": granted}),
    )
    registry = ProviderRegistry(database, settings)
    provider = _FakeInstagramProvider(settings, granted)
    registry.set_provider(Platform.instagram, provider)
    app = FastAPI()
    app.state.settings = settings
    app.include_router(
        build_platform_router(database, settings, registry=registry, oauth_states=oauth_states)
    )
    client = TestClient(app)
    response = client.get(
        f"/api/platforms/instagram/oauth/callback?code=abc&state={issued.state}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert provider.requested_scopes == granted
    with database.session() as session:
        stored = session.get(ConnectedAccount, account.id)
        assert stored is not None
        assert set(json.loads(stored.granted_scopes_json)) == set(granted)


def test_provider_auth_url_accepts_an_explicit_scope_list(tmp_path: Path) -> None:
    provider = InstagramProvider(
        Settings(instagram_app_id="ig-app", instagram_app_secret="ig-secret")
    )
    url = provider.authorization_url(
        state="s",
        redirect_uri="http://x/cb",
        code_challenge=None,
        scopes=list(INSTAGRAM_SCOPES) + [INSTAGRAM_PUBLISH_SCOPE],
    )
    query = parse_qs(urlparse(url).query)
    assert query["scope"] == [",".join(INSTAGRAM_SCOPES + (INSTAGRAM_PUBLISH_SCOPE,))]


def test_exchange_falls_back_to_the_states_requested_scopes(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.instagram.com":
            return httpx.Response(200, json={"access_token": "short", "user_id": 1})
        if request.url.path == "/access_token":
            return httpx.Response(200, json={"access_token": "long", "expires_in": 5184000})
        return httpx.Response(404, json={})

    provider = InstagramProvider(
        Settings(instagram_app_id="ig-app", instagram_app_secret="ig-secret"),
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    requested = list(INSTAGRAM_SCOPES) + [INSTAGRAM_PUBLISH_SCOPE]
    token = provider.exchange_code(
        code="c",
        redirect_uri="http://x/cb",
        code_verifier=None,
        requested_scopes=requested,
    )
    assert token.scopes == requested


# ------------------------------------------------------- oauth state payload


def test_oauth_state_carries_a_payload(database: Database) -> None:
    service = OAuthStateService(database)
    issued = service.issue(
        Platform.instagram,
        redirect_uri="http://x/cb",
        use_pkce=False,
        payload='{"scopes": ["a"]}',
    )
    record = service.consume(Platform.instagram, state=issued.state)
    assert record.code_verifier == '{"scopes": ["a"]}'
    assert issued.code_verifier is None  # no PKCE was issued


# ------------------------------------------------------------- token upkeep


def _due_token(days_left: int, *, issued_days_ago: int = 30) -> TokenSet:
    return TokenSet(
        access_token="old-token",
        expires_at=datetime.now(UTC) + timedelta(days=days_left),
        extra={
            "issued_at": (datetime.now(UTC) - timedelta(days=issued_days_ago)).isoformat(),
        },
    )


def test_upkeep_schedules_a_refresh_job_at_day_50(
    tmp_path: Path, database: Database, settings: Settings
) -> None:
    """Each Instagram account gets one job for its token's day-50 instant.

    A token whose issue time cannot be told (no `issued_at`, no expiry) gets
    none: the scheduler cannot compute when day 50 falls.
    """
    due = _account(database, scopes=[], external_id="ig1")
    future = _account(database, scopes=[], external_id="ig2")
    unknown = _account(database, scopes=[], external_id="ig3")
    store = FileTokenStore(settings.resolved_data_dir / "secrets")
    # Day 50 already passed: the job is due immediately.
    store.store("tok-due", _due_token(5, issued_days_ago=55))
    # Day 50 is still ahead: the job waits for its instant.
    store.store("tok-future", _due_token(30, issued_days_ago=30))
    store.store("tok-unknown", TokenSet(access_token="no-age-info"))
    for account, ref in (
        (due, "tok-due"),
        (future, "tok-future"),
        (unknown, "tok-unknown"),
    ):
        with database.session() as session:
            row = session.get(ConnectedAccount, account.id)
            assert row is not None
            row.token_ref = ref
    queue = JobQueue(database)
    registry = ProviderRegistry(database, settings)
    now = datetime.now(UTC)
    TokenUpkeepScheduler(database, registry, queue).produce(now)
    with database.session() as session:
        rows = session.scalars(select(PublishJob).where(PublishJob.kind == REFRESH_TOKEN_JOB)).all()
    by_account = {json.loads(row.payload_json)["account_id"]: row for row in rows}
    assert set(by_account) == {due.id, future.id}
    due_job = by_account[due.id]
    future_job = by_account[future.id]
    assert due_job.run_at.replace(tzinfo=UTC) <= now
    assert future_job.run_at.replace(tzinfo=UTC) > now
    assert (due_job.idempotency_key or "").startswith(f"{REFRESH_TOKEN_JOB}:{due.id}:")


class _RefreshingProvider:
    platform = Platform.instagram

    def __init__(self) -> None:
        self.refreshed = 0

    def refresh(self, token: TokenSet) -> TokenSet:
        self.refreshed += 1
        return token.model_copy(
            update={
                "access_token": "new-token",
                "expires_at": datetime.now(UTC) + timedelta(days=60),
            }
        )


class _FakeJob:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.id = 1


def test_refresh_job_renews_the_stored_token(
    tmp_path: Path, database: Database, settings: Settings
) -> None:
    account = _account(database, scopes=[], external_id="ig1")
    store = FileTokenStore(settings.resolved_data_dir / "secrets")
    store.store("tok-due", _due_token(10))
    with database.session() as session:
        row = session.get(ConnectedAccount, account.id)
        assert row is not None
        row.token_ref = "tok-due"
    registry = ProviderRegistry(database, settings)
    provider = _RefreshingProvider()
    registry.set_provider(Platform.instagram, provider)  # type: ignore[arg-type]
    run_refresh_token_job(registry, _FakeJob(payload={"account_id": account.id}))
    assert provider.refreshed == 1
    stored = store.load("tok-due")
    assert stored is not None and stored.access_token == "new-token"


class _DoomedProvider:
    platform = Platform.instagram

    def refresh(self, token: TokenSet) -> TokenSet:
        raise AuthorizationExpired("grant removed", status=401)


def test_refresh_job_marks_a_revoked_account_needs_reauth(
    tmp_path: Path, database: Database, settings: Settings
) -> None:
    account = _account(database, scopes=[], external_id="ig1")
    store = FileTokenStore(settings.resolved_data_dir / "secrets")
    store.store("tok-due", _due_token(10))
    with database.session() as session:
        row = session.get(ConnectedAccount, account.id)
        assert row is not None
        row.token_ref = "tok-due"
    registry = ProviderRegistry(database, settings)
    registry.set_provider(Platform.instagram, _DoomedProvider())  # type: ignore[arg-type]
    run_refresh_token_job(registry, _FakeJob(payload={"account_id": account.id}))
    with database.session() as session:
        stored = session.get(ConnectedAccount, account.id)
        assert stored is not None
        assert stored.status == "needs_reauth"


def test_refresh_job_skips_unified_tokens(
    tmp_path: Path, database: Database, settings: Settings
) -> None:
    """Unified Login tokens are Meta user tokens, not ig_* long-lived tokens:
    the scheduler never enqueues ig_refresh_token upkeep for them."""
    account = _account(database, scopes=[], external_id="ig1")
    store = FileTokenStore(settings.resolved_data_dir / "secrets")
    token = _due_token(10)
    token.extra["login"] = "unified"
    store.store("tok-due", token)
    with database.session() as session:
        row = session.get(ConnectedAccount, account.id)
        assert row is not None
        row.token_ref = "tok-due"
    queue = JobQueue(database)
    registry = ProviderRegistry(database, settings)
    TokenUpkeepScheduler(database, registry, queue).produce(datetime.now(UTC))
    with database.session() as session:
        assert (
            session.scalar(select(PublishJob).where(PublishJob.kind == REFRESH_TOKEN_JOB)) is None
        )


# ------------------------------------------------------------- scope mapping


def test_unified_scope_names_count_as_their_business_equivalents() -> None:
    from runway.platforms.capabilities import (
        Capability,
        capability_matrix,
        capability_ready,
        equivalent_scopes,
    )

    assert equivalent_scopes(["instagram_content_publish"]) >= {
        INSTAGRAM_PUBLISH_SCOPE,
    }
    # comments_read needs the basic and manage-comments scopes; the Unified
    # Login names satisfy it.
    entry = next(
        entry
        for entry in capability_matrix(Platform.instagram)
        if entry.capability is Capability.comments_read
    )
    assert capability_ready(
        entry,
        account_status="connected",
        granted_scopes=["instagram_basic", "instagram_manage_comments"],
    )


def test_publish_capability_accepts_the_unified_scope_family() -> None:
    from runway.platforms.publish_capability import (
        AccountFacts,
        AppStatus,
        publishing_capability,
    )
    from runway.posts.types import TargetPostType

    result = publishing_capability(
        TargetPostType.INSTAGRAM_REEL,
        account=AccountFacts("connected", ("instagram_content_publish",)),
        app=AppStatus("unverified", "development", "sandbox", False),
        profile="local",
        implemented={"instagram_direct", "instagram_unified", "assisted"},
        media_host_available=True,
    )
    assert result.state == "ready"
