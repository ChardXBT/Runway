"""Instagram Unified Login (`instagram_unified`, spec 08 section 3).

Unified Login uses Facebook Login for Business with a `config_id`: the OAuth
dialog is `facebook.com/dialog/oauth`, the exchange and every read hit
`graph.facebook.com`, and a Business Integration System User token is issued.
`GET /me/assigned_instagram_accounts` lists the professional accounts the
owner may connect; the owner picks one when several are assigned. Reels and
videos upload from disk through `rupload.facebook.com` (`upload_type=
resumable`); images still need a public `image_url`.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from runway.api.platform_routes import build_platform_router
from runway.auth.principal import get_principal, local_owner
from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import ConnectedAccount, OAuthState
from runway.platforms.base import TokenSet
from runway.platforms.capabilities import Platform
from runway.platforms.instagram import API_VERSION, InstagramProvider
from runway.platforms.instagram_publisher import (
    InstagramDirectPublisher,
    InstagramUnifiedPublisher,
)
from runway.platforms.instagram_unified import (
    UNIFIED_SCOPES,
    InstagramUnifiedProvider,
)
from runway.platforms.oauth import OAuthStateService
from runway.platforms.publisher import (
    AccountRef,
    MediaRef,
    PublishContext,
    PublishRefused,
)
from runway.platforms.publisher_registry import (
    LIVE_PUBLISHER_NAMES,
    build_publisher_registry,
)
from runway.platforms.registry import ProviderRegistry

META_APP = "meta-app"
META_SECRET = "meta-secret"
CONFIG_ID = "cfg-12345"
IG = "17841400923817"
IG2 = "17841400923818"
FB_GRAPH = f"https://graph.facebook.com/{API_VERSION}"
NOW = datetime(2026, 12, 1, 15, 0, tzinfo=UTC)


def migrated(settings: Settings) -> Settings:
    initialize_database(settings).engine.dispose()
    return settings


def _unified_settings(tmp_path: Path) -> Settings:
    return migrated(
        Settings(
            data_dir=tmp_path / "data",
            meta_app_id=META_APP,
            meta_app_secret=META_SECRET,
            instagram_unified_config_id=CONFIG_ID,
        )
    )


# ------------------------------------------------------------------- settings


def test_unified_needs_meta_app_and_config_id(tmp_path: Path) -> None:
    base = {"data_dir": tmp_path / "data", "instagram_unified_config_id": CONFIG_ID}
    assert not InstagramUnifiedProvider(Settings(**base)).configured()
    assert not InstagramUnifiedProvider(Settings(**base, meta_app_id=META_APP)).configured()
    provider = InstagramUnifiedProvider(
        Settings(**base, meta_app_id=META_APP, meta_app_secret=META_SECRET)
    )
    assert provider.configured()


# ------------------------------------------------------------- provider OAuth


def test_authorization_url_uses_config_id_and_no_scope() -> None:
    provider = InstagramUnifiedProvider(
        Settings(
            meta_app_id=META_APP,
            meta_app_secret=META_SECRET,
            instagram_unified_config_id=CONFIG_ID,
        )
    )
    url = provider.authorization_url(
        state="s1", redirect_uri="https://edge.test/cb", code_challenge=None
    )
    parsed = urlparse(url)
    assert parsed.netloc == "www.facebook.com"
    assert parsed.path == "/dialog/oauth"
    query = parse_qs(parsed.query)
    assert query["client_id"] == [META_APP]
    assert query["config_id"] == [CONFIG_ID]
    assert query["response_type"] == ["code"]
    assert query["redirect_uri"] == ["https://edge.test/cb"]
    assert "scope" not in query  # permissions come from the configuration


class FakeFacebook:
    """graph.facebook.com endpoints the unified flow calls, over MockTransport."""

    def __init__(self) -> None:
        self.assigned: list[dict[str, str]] = [{"id": IG, "username": "northwindstudio"}]
        self.client_business_id = "biz-9"
        self.requests: list[tuple[str, str, str]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        if path.startswith(API_VERSION + "/"):
            path = path[len(API_VERSION) + 1 :]
        query = parse_qs(request.url.query.decode()) if request.url.query else {}
        self.requests.append((request.method, request.url.host, path))
        if path == "oauth/access_token":
            # The exchange authenticates with client_id/client_secret query
            # params, not a Bearer header.
            return httpx.Response(200, json={"access_token": "sys-token", "token_type": "bearer"})
        # Every other graph call must authenticate like the real API: a
        # request without the held token's Bearer header is an OAuth failure.
        if request.headers.get("Authorization") != "Bearer sys-token":
            return httpx.Response(
                401,
                json={
                    "error": {
                        "code": 190,
                        "message": "Invalid OAuth access token - cannot parse access token",
                    }
                },
            )
        if path == "me" and query.get("fields") == ["client_business_id"]:
            return httpx.Response(
                200, json={"id": "sys-1", "client_business_id": self.client_business_id}
            )
        if path == "me/assigned_instagram_accounts":
            return httpx.Response(200, json={"data": self.assigned})
        if path == IG:
            return httpx.Response(
                200,
                json={
                    "id": IG,
                    "username": "northwindstudio",
                    "name": "Northwind Studio",
                    "account_type": "BUSINESS",
                },
            )
        if path == IG2:
            return httpx.Response(
                200,
                json={
                    "id": IG2,
                    "username": "secondig",
                    "account_type": "BUSINESS",
                },
            )
        if path == f"{IG}/media":
            return httpx.Response(200, json={"data": []})
        return httpx.Response(404, json={"error": {"code": 100, "message": f"no {path}"}})


def _unified_provider(graph: FakeFacebook, settings: Settings) -> InstagramUnifiedProvider:
    return InstagramUnifiedProvider(
        settings, client=httpx.Client(transport=httpx.MockTransport(graph.handler))
    )


def test_exchange_hits_facebook_with_the_meta_app(tmp_path: Path) -> None:
    graph = FakeFacebook()
    settings = _unified_settings(tmp_path)
    provider = _unified_provider(graph, settings)
    token = provider.exchange_code(
        code="code-1", redirect_uri="https://edge.test/cb", code_verifier=None
    )
    assert token.access_token == "sys-token"
    assert token.extra["login"] == "unified"
    assert token.extra["config_id"] == CONFIG_ID
    method, host, path = graph.requests[0]
    assert host == "graph.facebook.com"
    assert path == "oauth/access_token"
    # No long-lived exchange call: the configuration sets the lifetime.
    assert len(graph.requests) == 1


def test_client_business_id_and_assigned_accounts(tmp_path: Path) -> None:
    graph = FakeFacebook()
    settings = _unified_settings(tmp_path)
    provider = _unified_provider(graph, settings)
    token = provider.exchange_code(
        code="c", redirect_uri="https://edge.test/cb", code_verifier=None
    )
    token = provider.attach_business_id(token)
    assert token.extra["client_business_id"] == "biz-9"
    assert provider.assigned_accounts(token) == [{"id": IG, "username": "northwindstudio"}]


# ------------------------------------------------------- unified read branch


def test_unified_reads_use_facebook_graph_and_the_account_id(
    tmp_path: Path,
) -> None:
    graph = FakeFacebook()
    provider = InstagramProvider(
        Settings(), client=httpx.Client(transport=httpx.MockTransport(graph.handler))
    )
    token = TokenSet(
        access_token="sys-token",
        extra={"login": "unified", "user_id": IG, "config_id": CONFIG_ID},
    )
    identity = provider.fetch_identity(token)
    assert identity.external_account_id == IG
    assert identity.handle == "northwindstudio"
    page = provider.list_content(token, cursor=None, limit=5)
    assert page.items == []
    methods_hosts = [(m, h, p) for m, h, p in graph.requests]
    assert all(host == "graph.facebook.com" for _m, host, _p in methods_hosts)
    assert ("GET", "graph.facebook.com", IG) in methods_hosts
    assert ("GET", "graph.facebook.com", f"{IG}/media") in methods_hosts
    assert not any(path == "me" or path.startswith("me/") for _m, _h, path in methods_hosts)


def test_direct_reads_keep_graph_instagram_and_me(tmp_path: Path) -> None:
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.path.lstrip("/")))
        if request.url.path.endswith("/media"):
            return httpx.Response(200, json={"data": []})
        return httpx.Response(
            200,
            json={"user_id": IG, "username": "northwindstudio", "account_type": "BUSINESS"},
        )

    provider = InstagramProvider(
        Settings(), client=httpx.Client(transport=httpx.MockTransport(handler))
    )
    provider.list_content(TokenSet(access_token="ig-tok"), cursor=None, limit=5)
    assert seen == [("graph.instagram.com", f"{API_VERSION}/me/media")]


# --------------------------------------------------------------- connect flow


def _router_app(
    database: Database,
    settings: Settings,
    *,
    registry: ProviderRegistry | None = None,
    oauth_states: OAuthStateService | None = None,
    unified: InstagramUnifiedProvider | None = None,
) -> FastAPI:
    app = FastAPI()
    app.state.settings = settings
    # Pin the principal: identical to what the local profile resolves, and it
    # keeps the hosted-profile tests free of the hosted identity stack.
    app.dependency_overrides[get_principal] = local_owner
    app.include_router(
        build_platform_router(
            database,
            settings,
            registry=registry,
            oauth_states=oauth_states,
            unified_provider=unified,
        )
    )
    return app


def test_connect_can_issue_a_unified_url(tmp_path: Path) -> None:
    settings = _unified_settings(tmp_path)
    database = Database(settings)
    oauth_states = OAuthStateService(database)
    app = _router_app(
        database,
        settings,
        registry=ProviderRegistry(database, settings),
        oauth_states=oauth_states,
        unified=InstagramUnifiedProvider(settings),
    )
    client = TestClient(app)
    response = client.post(
        "/api/platforms/instagram/connect", json={"adapter": "instagram_unified"}
    )
    assert response.status_code == 200
    url = response.json()["authorization_url"]
    query = parse_qs(urlparse(url).query)
    assert query["config_id"] == [CONFIG_ID]
    assert query["client_id"] == [META_APP]
    state = response.json()["state"]
    with database.session() as session:
        record = session.scalar(select(OAuthState).where(OAuthState.state == state))
        assert record is not None
        meta = json.loads(record.code_verifier or "{}")
        assert meta["adapter"] == "instagram_unified"


def test_unified_connect_needs_configuration(tmp_path: Path) -> None:
    settings = migrated(Settings(data_dir=tmp_path / "data"))
    database = Database(settings)
    app = _router_app(database, settings)
    response = TestClient(app).post(
        "/api/platforms/instagram/connect", json={"adapter": "instagram_unified"}
    )
    assert response.status_code == 409


def _issue_unified_state(database: Database, oauth_states: OAuthStateService) -> str:
    return oauth_states.issue(
        Platform.instagram,
        redirect_uri="http://127.0.0.1:8000/api/platforms/instagram/oauth/callback",
        use_pkce=False,
        payload=json.dumps({"adapter": "instagram_unified"}),
    ).state


def _callback_app(
    tmp_path: Path,
    graph: FakeFacebook,
    settings: Settings | None = None,
) -> tuple[Settings, Database, OAuthStateService, TestClient, ProviderRegistry]:
    settings = settings or _unified_settings(tmp_path)
    database = Database(settings)
    oauth_states = OAuthStateService(database)
    registry = ProviderRegistry(database, settings)
    # The base provider serves the read side of the callback (fetch_identity)
    # over graph.facebook.com for the unified token.
    registry.set_provider(
        Platform.instagram,
        InstagramProvider(
            settings, client=httpx.Client(transport=httpx.MockTransport(graph.handler))
        ),
    )
    app = _router_app(
        database,
        settings,
        registry=registry,
        oauth_states=oauth_states,
        unified=_unified_provider(graph, settings),
    )
    return settings, database, oauth_states, TestClient(app), registry


def test_unified_callback_connects_the_only_assigned_account(
    tmp_path: Path,
) -> None:
    graph = FakeFacebook()
    settings, database, oauth_states, client, registry = _callback_app(tmp_path, graph)
    state = _issue_unified_state(database, oauth_states)
    response = client.get(
        f"/api/platforms/instagram/oauth/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "connected=instagram" in response.headers["location"]
    with database.session() as session:
        account = session.scalar(
            select(ConnectedAccount).where(ConnectedAccount.platform == "instagram")
        )
        assert account is not None
        assert account.external_account_id == IG
        assert account.handle == "northwindstudio"
        meta = json.loads(account.metadata_json or "{}")
        assert meta["connection"] == "instagram_unified"
        token_ref = account.token_ref
    assert token_ref is not None
    token = registry.token_store.load(token_ref)
    assert token is not None
    assert token.extra["login"] == "unified"
    assert token.extra["config_id"] == CONFIG_ID
    assert token.extra["client_business_id"] == "biz-9"
    assert token.extra["user_id"] == IG
    assert token.scopes == list(UNIFIED_SCOPES)


def test_unified_callback_with_several_accounts_waits_for_a_pick(
    tmp_path: Path,
) -> None:
    graph = FakeFacebook()
    graph.assigned = [
        {"id": IG, "username": "northwindstudio"},
        {"id": IG2, "username": "secondig"},
    ]
    settings, database, oauth_states, client, registry = _callback_app(tmp_path, graph)
    state = _issue_unified_state(database, oauth_states)
    response = client.get(
        f"/api/platforms/instagram/oauth/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    location = response.headers["location"]
    assert "select_instagram=" in location
    select_state = parse_qs(urlparse(location).query)["select_instagram"][0]
    # Nothing is persisted before the owner picks.
    with database.session() as session:
        assert session.scalar(select(ConnectedAccount)) is None
    listed = client.get(f"/api/platforms/instagram/unified/select?state={select_state}")
    assert listed.status_code == 200
    assert [a["id"] for a in listed.json()["accounts"]] == [IG, IG2]
    picked = client.post(
        "/api/platforms/instagram/unified/select",
        json={"state": select_state, "external_account_id": IG2},
    )
    assert picked.status_code == 200
    assert picked.json()["status"] == "connected"
    with database.session() as session:
        account = session.scalar(select(ConnectedAccount))
        assert account is not None
        assert account.external_account_id == IG2
        assert account.handle == "secondig"
        token_ref = account.token_ref
    assert token_ref is not None
    token = registry.token_store.load(token_ref)
    assert token is not None and token.extra["user_id"] == IG2
    # A second pick with the consumed state is rejected.
    again = client.post(
        "/api/platforms/instagram/unified/select",
        json={"state": select_state, "external_account_id": IG},
    )
    assert again.status_code == 404


def _two_account_select(
    tmp_path: Path,
    graph: FakeFacebook,
    settings: Settings | None = None,
    extra_assigned: list[dict[str, str]] | None = None,
) -> tuple[Database, TestClient, ProviderRegistry, str]:
    graph.assigned = [
        {"id": IG, "username": "northwindstudio"},
        {"id": IG2, "username": "secondig"},
        *(extra_assigned or []),
    ]
    _s, database, oauth_states, client, registry = _callback_app(tmp_path, graph, settings)
    state = _issue_unified_state(database, oauth_states)
    response = client.get(
        f"/api/platforms/instagram/oauth/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    select_state = parse_qs(urlparse(response.headers["location"]).query)["select_instagram"][0]
    return database, client, registry, select_state


def _held_token_ref(database: Database, select_state: str) -> str:
    with database.session() as session:
        record = session.scalar(select(OAuthState).where(OAuthState.state == select_state))
        assert record is not None
        return str(json.loads(record.code_verifier or "{}")["token_ref"])


def test_unified_select_rejects_a_bad_pick_without_consuming_the_state(
    tmp_path: Path,
) -> None:
    database, client, _r, select_state = _two_account_select(tmp_path, FakeFacebook())
    bad = client.post(
        "/api/platforms/instagram/unified/select",
        json={"state": select_state, "external_account_id": "not-assigned"},
    )
    assert bad.status_code == 400
    # The state was not consumed by the rejected pick: a valid one works.
    good = client.post(
        "/api/platforms/instagram/unified/select",
        json={"state": select_state, "external_account_id": IG2},
    )
    assert good.status_code == 200
    with database.session() as session:
        account = session.scalar(select(ConnectedAccount))
        assert account is not None and account.external_account_id == IG2


def test_unified_select_deletes_the_held_token(tmp_path: Path) -> None:
    database, client, registry, select_state = _two_account_select(tmp_path, FakeFacebook())
    held_ref = _held_token_ref(database, select_state)
    assert registry.token_store.load(held_ref) is not None
    picked = client.post(
        "/api/platforms/instagram/unified/select",
        json={"state": select_state, "external_account_id": IG2},
    )
    assert picked.status_code == 200
    assert registry.token_store.load(held_ref) is None


def test_unified_select_maps_a_persist_failure_to_the_fail_redirect(
    tmp_path: Path,
) -> None:
    graph = FakeFacebook()
    # The third assigned account has no identity endpoint: fetch_identity
    # raises ProviderError inside _persist_connection.
    database, client, registry, select_state = _two_account_select(
        tmp_path,
        graph,
        extra_assigned=[{"id": "17841400923819", "username": "thirdig"}],
    )
    held_ref = _held_token_ref(database, select_state)
    picked = client.post(
        "/api/platforms/instagram/unified/select",
        json={"state": select_state, "external_account_id": "17841400923819"},
        follow_redirects=False,
    )
    assert picked.status_code == 303
    assert "error=exchange_failed" in picked.headers["location"]
    assert registry.token_store.load(held_ref) is None
    with database.session() as session:
        assert session.scalar(select(ConnectedAccount)) is None


def test_unified_select_maps_a_quota_problem_to_the_fail_redirect(
    tmp_path: Path,
) -> None:
    # Quotas only bind in the hosted profile; model_copy skips the hosted
    # profile_requirements validator (Postgres, identity provider, ...).
    settings = migrated(
        Settings(
            data_dir=tmp_path / "data",
            meta_app_id=META_APP,
            meta_app_secret=META_SECRET,
            instagram_unified_config_id=CONFIG_ID,
        ).model_copy(update={"profile": "hosted", "quota_connected_accounts": 0})
    )
    database, client, registry, select_state = _two_account_select(
        tmp_path, FakeFacebook(), settings
    )
    held_ref = _held_token_ref(database, select_state)
    picked = client.post(
        "/api/platforms/instagram/unified/select",
        json={"state": select_state, "external_account_id": IG2},
        follow_redirects=False,
    )
    assert picked.status_code == 303
    assert "error=quota_exceeded" in picked.headers["location"]
    assert registry.token_store.load(held_ref) is None


def test_unified_callback_without_assigned_accounts_fails(tmp_path: Path) -> None:
    graph = FakeFacebook()
    graph.assigned = []
    _settings, database, oauth_states, client, _r = _callback_app(tmp_path, graph)
    state = _issue_unified_state(database, oauth_states)
    response = client.get(
        f"/api/platforms/instagram/oauth/callback?code=abc&state={state}",
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "error=no_instagram_accounts" in response.headers["location"]


# -------------------------------------------------------- capability adapter


def test_capability_picks_unified_for_a_unified_account() -> None:
    from runway.platforms.publish_capability import (
        AccountFacts,
        AppStatus,
        publishing_capability,
    )
    from runway.posts.types import TargetPostType

    unified = publishing_capability(
        TargetPostType.INSTAGRAM_REEL,
        account=AccountFacts(
            "connected",
            ("instagram_content_publish",),
            connection="instagram_unified",
        ),
        app=AppStatus("unverified", "development", "sandbox", False),
        profile="local",
        implemented={"instagram_direct", "instagram_unified", "assisted"},
        media_host_available=False,
    )
    assert unified.direct_adapter == "instagram_unified"
    assert unified.default_publisher == "instagram_unified"
    assert unified.state == "ready"  # Reels upload from disk; no media host needed
    direct = publishing_capability(
        TargetPostType.INSTAGRAM_REEL,
        account=AccountFacts(
            "connected", ("instagram_business_content_publish",), connection="instagram"
        ),
        app=AppStatus("unverified", "development", "sandbox", False),
        profile="local",
        implemented={"instagram_direct", "instagram_unified", "assisted"},
        media_host_available=False,
    )
    assert direct.direct_adapter == "instagram_direct"
    assert direct.state == "needs_media_host"


def test_capability_keeps_media_host_for_post_types_that_can_hold_images() -> None:
    from runway.platforms.publish_capability import (
        AccountFacts,
        AppStatus,
        publishing_capability,
    )
    from runway.posts.types import TargetPostType

    # The capability check sees only the post type, not the media kinds: a
    # story or carousel can carry images, which still need a public URL even
    # through instagram_unified. Only the always-video Reel is exempt.
    for post_type in (TargetPostType.INSTAGRAM_STORY, TargetPostType.INSTAGRAM_CAROUSEL):
        capability = publishing_capability(
            post_type,
            account=AccountFacts(
                "connected",
                ("instagram_content_publish",),
                connection="instagram_unified",
            ),
            app=AppStatus("unverified", "development", "sandbox", False),
            profile="local",
            implemented={"instagram_direct", "instagram_unified", "assisted"},
            media_host_available=False,
        )
        assert capability.state == "needs_media_host"


# ----------------------------------------------------------------- publisher


class FakeMetaGraph(FakeFacebook):
    """Adds containers/media_publish/rupload to the FakeFacebook surface."""

    def __init__(self) -> None:
        super().__init__()
        self.next_container = 0
        self.containers: dict[str, dict[str, object]] = {}
        self.media_counter = 0
        self.media_rows: dict[str, dict[str, object]] = {}
        self.media_order: list[str] = []
        self.quota_usage = 0
        self.quota_total: int | None = 50
        self.uploads: list[tuple[str, str, str, int]] = []  # (cid, offset, size, len)
        self.upload_responses: list[httpx.Response] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "rupload.facebook.com":
            # The resumable upload authenticates with `Authorization: OAuth`.
            if request.headers.get("Authorization") != "OAuth sys-token":
                return httpx.Response(
                    401,
                    json={
                        "error": {
                            "code": 190,
                            "message": "Invalid OAuth access token",
                        }
                    },
                )
            parts = request.url.path.lstrip("/").split("/")
            container_id = parts[-1]
            offset = request.headers.get("offset", "0")
            size = request.headers.get("file_size", "0")
            self.uploads.append((container_id, offset, size, len(request.content)))
            if self.upload_responses:
                return self.upload_responses.pop(0)
            return httpx.Response(200, json={"h": "ok"})
        path = request.url.path.lstrip("/")
        if path.startswith(API_VERSION + "/"):
            path = path[len(API_VERSION) + 1 :]
        method = request.method
        body = parse_qs(request.content.decode()) if request.content else {}
        self.requests.append((method, request.url.host, path))
        if path == "oauth/access_token":
            return super().handler(request)
        # Every graph call must authenticate like the real API: a request
        # without the held token's Bearer header is an OAuth failure (190).
        if request.headers.get("Authorization") != "Bearer sys-token":
            return httpx.Response(
                401,
                json={
                    "error": {
                        "code": 190,
                        "message": "Invalid OAuth access token - cannot parse access token",
                    }
                },
            )
        if path.endswith("/content_publishing_limit"):
            # The real endpoint wraps the record in `data` and returns `config`
            # only when the fields parameter asks for it.
            entry: dict[str, object] = {"quota_usage": self.quota_usage}
            fields = str(request.url.params.get("fields", ""))
            if "config" in fields and self.quota_total is not None:
                entry["config"] = {"quota_total": self.quota_total}
            return httpx.Response(200, json={"data": [entry]})
        if path.endswith("/media") and method == "POST":
            self.next_container += 1
            container_id = f"c{self.next_container}"
            self.containers[container_id] = {
                "status_code": "FINISHED",
                "params": {k: v[0] for k, v in body.items()},
            }
            return httpx.Response(200, json={"id": container_id})
        if path.endswith("/media") and method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        self.media_rows[mid] for mid in self.media_order if mid in self.media_rows
                    ]
                },
            )
        if path.endswith("/media_publish") and method == "POST":
            self.media_counter += 1
            media_id = f"media{self.media_counter}"
            self.media_rows[media_id] = {
                "id": media_id,
                "permalink": f"https://www.instagram.com/p/{media_id}/",
                "media_type": "REELS",
                "timestamp": "2026-12-01T15:00:10+0000",
            }
            self.media_order.insert(0, media_id)
            return httpx.Response(200, json={"id": media_id})
        if path in self.containers:
            container = self.containers[path]
            return httpx.Response(
                200,
                json={
                    "id": path,
                    "status_code": container["status_code"],
                    "status": container.get("status", ""),
                },
            )
        if path in self.media_rows:
            return httpx.Response(200, json=self.media_rows[path])
        return super().handler(request)


def _media(
    kind: str = "video",
    *,
    storage: str = "local",
    key: str = "w/1/7/aaaabbbbccccdddd.mp4",
    size: int = 12345,
    position: int = 0,
) -> MediaRef:
    return MediaRef(
        asset_id=7 + position,
        position=position,
        kind=kind,
        sha256="a" * 64,
        size_bytes=size,
        content_type="video/mp4" if kind == "video" else "image/jpeg",
        storage=storage,
        storage_key=key,
        original_filename=f"item{position}.{kind}",
    )


def _context(
    post_type: str = "instagram_reel",
    *,
    media: tuple[MediaRef, ...] | None = None,
    settings: dict | None = None,
    resumable: dict | None = None,
    caption: str = "a caption",
    signed: dict[str, str] | None = None,
    login: str = "unified",
    scopes: tuple[str, ...] = ("instagram_content_publish",),
    token: TokenSet | None = None,
) -> tuple[PublishContext, dict[str, int], list[dict]]:
    marks = {"count": 0}
    saves: list[dict] = []

    def mark() -> datetime:
        marks["count"] += 1
        return datetime.now(UTC)

    signed_map = signed if signed is not None else {}
    held_token = token or TokenSet(
        access_token="sys-token",
        extra={"user_id": IG, "login": login, "config_id": CONFIG_ID},
    )
    return (
        PublishContext(
            workspace_id=1,
            post_id=1,
            target_id=7,
            platform=Platform.instagram,
            post_type=post_type,
            publisher="instagram_unified",
            account=AccountRef(
                id=3,
                platform=Platform.instagram,
                external_account_id=IG,
                handle="northwindstudio",
                status="connected",
                granted_scopes=scopes,
                token_ref="tok-ref",
                account_type="BUSINESS",
            ),
            title="T",
            caption=caption,
            settings=settings or {},
            media=media if media is not None else (_media(),),
            planned_at=NOW + timedelta(minutes=20),
            payload_hash="b" * 64,
            ai_generated=False,
            resumable=dict(resumable or {}),
            clock=lambda: NOW,
            _mark_request_sent=mark,
            _save_resumable=lambda s: saves.append(dict(s)),
            _open_media=lambda m: io.BytesIO(b"\x00" * m.size_bytes),
            _signed_media_url=lambda m, ttl: signed_map.get(m.storage_key),
            _access_token=lambda: held_token,
        ),
        marks,
        saves,
    )


def _unified_publisher(
    graph: FakeMetaGraph,
    *,
    settings: Settings | None = None,
    r2: object = None,
) -> InstagramUnifiedPublisher:
    client = httpx.Client(transport=httpx.MockTransport(graph.handler))
    return InstagramUnifiedPublisher(
        settings or Settings(data_dir=Path("unused")),
        Database(Settings(data_dir=Path("unused2"))),
        client=client,
        upload_client=client,
        r2=r2,  # type: ignore[arg-type]
        sleep=lambda _s: None,
    )


def test_unified_registered_in_the_live_build(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    registry = build_publisher_registry(settings, Database(settings))
    assert "instagram_unified" in registry.names()
    assert "instagram_unified" in LIVE_PUBLISHER_NAMES
    assert registry.require("instagram_unified").platform is Platform.instagram
    assert registry.require("instagram_unified").supports("instagram_reel")


def test_reel_publishes_via_resumable_upload() -> None:
    graph = FakeMetaGraph()
    publisher = _unified_publisher(graph)
    ctx, marks, _saves = _context()
    result = publisher.send(ctx, None)
    posts = [
        r
        for r in graph.requests
        if r[2].endswith("/media") and r[0] == "POST" and r[1] == "graph.facebook.com"
    ]
    assert len(posts) == 1
    params = graph.containers["c1"]["params"]
    assert params["media_type"] == "REELS"
    assert params["upload_type"] == "resumable"
    assert "video_url" not in params
    assert len(graph.uploads) == 1
    container_id, offset, size, length = graph.uploads[0]
    assert container_id == "c1"
    assert offset == "0"
    assert size == "12345"
    assert length == 12345
    assert result.status == "published"
    assert result.external_id == "media1"
    assert marks["count"] == 1


def test_upload_resumes_from_the_reported_offset() -> None:
    graph = FakeMetaGraph()
    graph.upload_responses = [
        httpx.Response(400, headers={"offset": "100"}, json={"error": "interrupted"})
    ]
    publisher = _unified_publisher(graph)
    ctx, _m, _s = _context()
    result = publisher.send(ctx, None)
    assert result.status == "published"
    assert len(graph.uploads) == 2
    assert graph.uploads[1][1] == "100"
    assert graph.uploads[1][3] == 12345 - 100


def test_images_still_use_a_public_url() -> None:
    graph = FakeMetaGraph()
    publisher = _unified_publisher(graph)
    ctx, _m, _s = _context(
        post_type="instagram_image",
        media=(_media("image", key="w/1/7/i1i1i1i1i1i1i1i1.jpg"),),
        signed={"w/1/7/i1i1i1i1i1i1i1i1.jpg": "https://sig.test/i"},
    )
    publisher.send(ctx, None)
    params = graph.containers["c1"]["params"]
    assert params["image_url"] == "https://sig.test/i"
    assert "upload_type" not in params
    assert graph.uploads == []


def test_preflight_needs_no_media_host_for_video_only() -> None:
    graph = FakeMetaGraph()
    publisher = _unified_publisher(graph)  # no R2 configured
    video_ctx, _m, _s = _context(media=(_media("video", storage="local"),), signed={})
    codes = {issue.code for issue in publisher.preflight(video_ctx)}
    assert "media_host" not in codes
    image_ctx, _m2, _s2 = _context(
        post_type="instagram_image",
        media=(_media("image", storage="local", key="w/1/7/j1j1j1j1j1j1j1j1.jpg"),),
        signed={},
    )
    codes2 = {issue.code for issue in publisher.preflight(image_ctx)}
    assert "media_host" in codes2


def test_unified_publisher_refuses_a_direct_token() -> None:
    graph = FakeMetaGraph()
    publisher = _unified_publisher(graph)
    ctx, marks, _s = _context(login="instagram")
    with pytest.raises(PublishRefused) as caught:
        publisher.send(ctx, None)
    assert caught.value.code == "account_wrong_adapter"
    assert marks["count"] == 0


def test_direct_publisher_refuses_a_unified_token() -> None:
    graph = FakeMetaGraph()
    direct = InstagramDirectPublisher(
        Settings(data_dir=Path("unused")),
        Database(Settings(data_dir=Path("unused2"))),
        client=httpx.Client(transport=httpx.MockTransport(graph.handler)),
        upload_client=httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(200))),
        sleep=lambda _s: None,
    )
    ctx, marks, _s = _context()  # unified token
    with pytest.raises(PublishRefused) as caught:
        direct.send(ctx, None)
    assert caught.value.code == "account_wrong_adapter"
    assert marks["count"] == 0


def test_publishes_on_facebook_graph_not_instagram_graph() -> None:
    graph = FakeMetaGraph()
    publisher = _unified_publisher(graph)
    ctx, _m, _s = _context()
    publisher.send(ctx, None)
    hosts = {host for _m, host, _p in graph.requests}
    assert "graph.facebook.com" in hosts
    assert "graph.instagram.com" not in hosts


def test_status_publish_and_verify_round_trip() -> None:
    graph = FakeMetaGraph()
    graph.containers["c9"] = {"status_code": "FINISHED", "params": {}}
    publisher = _unified_publisher(graph)
    ctx, marks, _s = _context(
        resumable={"containers": [{"id": "c9", "position": 0, "role": "main"}]}
    )
    result = publisher.send(ctx, None)
    assert result.status == "published"
    publishes = [r for r in graph.requests if r[2].endswith("/media_publish") and r[0] == "POST"]
    assert len(publishes) == 1
    assert marks["count"] == 1
    ctx.external_id = result.external_id
    verify = publisher.verify(ctx)
    assert verify.verified is True
    assert verify.permalink == f"https://www.instagram.com/p/{result.external_id}/"
