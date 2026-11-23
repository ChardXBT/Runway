"""Token refresh: transient failures stay transient; refresh is serialized per account."""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import ConnectedAccount
from runway.platforms.base import AuthorizationExpired, ProviderError, TokenSet
from runway.platforms.capabilities import Platform
from runway.platforms.registry import ProviderRegistry
from runway.platforms.sync import SyncService
from runway.platforms.tiktok import TikTokProvider
from runway.platforms.tokens import TokenRefreshBusy, TokenStore
from runway.platforms.youtube import YouTubeProvider


@pytest.fixture()
def live(tmp_path: Path) -> Iterator[tuple[Settings, Database]]:
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="live",
        youtube_client_id="yt-client",
        youtube_client_secret="yt-secret",
        tiktok_client_key="tt-key",
        tiktok_client_secret="tt-secret",
    )
    database = initialize_database(settings)
    yield settings, database
    database.engine.dispose()


def _expiring(refresh_token: str = "refresh-1") -> TokenSet:
    return TokenSet(
        access_token="access-0",
        refresh_token=refresh_token,
        expires_at=datetime.now(UTC) + timedelta(minutes=1),
    )


def _account(database: Database, registry: ProviderRegistry, platform: str, token: TokenSet) -> int:
    token_ref = registry.token_store.new_token_ref()
    with database.session() as session:
        account = ConnectedAccount(
            platform=platform,
            external_account_id=f"{platform}-owner",
            status="connected",
            granted_scopes_json="[]",
            token_ref=token_ref,
        )
        session.add(account)
        session.flush()
        account_id = account.id
    registry.token_store.store(token_ref, token)
    return account_id


def _account_row(database: Database, account_id: int) -> ConnectedAccount:
    with database.session() as session:
        account = session.get(ConnectedAccount, account_id)
        assert account is not None
        session.expunge(account)
        return account


# ------------------------------------------------- failure classification (C2)


def test_dns_failure_during_refresh_leaves_the_account_connected(live) -> None:
    settings, database = live

    def unreachable(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("[Errno 11001] getaddrinfo failed", request=request)

    registry = ProviderRegistry(database, settings)
    registry.set_provider(
        Platform.youtube,
        YouTubeProvider(settings, httpx.Client(transport=httpx.MockTransport(unreachable))),
    )
    account_id = _account(database, registry, "youtube", _expiring())

    run = SyncService(database, settings, registry).run(account_id)

    assert run.status == "failed"
    assert "getaddrinfo" in (run.error_summary or "")
    account = _account_row(database, account_id)
    assert account.status == "connected"
    stored = registry.token_store.load(str(account.token_ref))
    assert stored is not None and stored.refresh_token == "refresh-1"


def test_token_endpoint_server_error_is_retryable_and_keeps_the_account(live) -> None:
    settings, database = live

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "backend_error"})

    registry = ProviderRegistry(database, settings)
    registry.set_provider(
        Platform.youtube,
        YouTubeProvider(settings, httpx.Client(transport=httpx.MockTransport(down))),
    )
    account_id = _account(database, registry, "youtube", _expiring())

    SyncService(database, settings, registry).run(account_id)

    assert _account_row(database, account_id).status == "connected"


def test_invalid_grant_from_the_token_endpoint_needs_reauth(live) -> None:
    settings, database = live

    def revoked(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid_grant"})

    registry = ProviderRegistry(database, settings)
    registry.set_provider(
        Platform.youtube,
        YouTubeProvider(settings, httpx.Client(transport=httpx.MockTransport(revoked))),
    )
    account_id = _account(database, registry, "youtube", _expiring())

    SyncService(database, settings, registry).run(account_id)

    assert _account_row(database, account_id).status == "needs_reauth"


@pytest.mark.parametrize(
    ("status", "payload", "expected"),
    [
        (400, {"error": "invalid_grant", "error_description": "refresh token expired"}, "reauth"),
        (401, {"error": "invalid_client"}, "reauth"),
        (500, {"error": "internal_error", "log_id": "x1"}, "retry"),
        (429, {"error": "rate_limit_exceeded"}, "retry"),
        (400, {"error": "invalid_request"}, "fail"),
    ],
)
def test_tiktok_token_endpoint_string_errors_are_classified(
    status: int, payload: dict[str, str], expected: str
) -> None:
    provider = TikTokProvider(
        Settings(tiktok_client_key="k", tiktok_client_secret="s"),
        httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(status, json=payload))
        ),
    )
    with pytest.raises(ProviderError) as raised:
        provider.refresh(_expiring())
    if expected == "reauth":
        assert isinstance(raised.value, AuthorizationExpired)
    else:
        assert not isinstance(raised.value, AuthorizationExpired)
        assert raised.value.retryable is (expected == "retry")


# ------------------------------------------------------ lifetimes from responses


def test_tiktok_refresh_takes_both_lifetimes_from_each_response() -> None:
    responses = iter(
        [
            {
                "access_token": "a1",
                "refresh_token": "r2",
                "expires_in": 7200,
                "refresh_expires_in": 86400 * 180,
                "open_id": "open-1",
            },
            # A later response with different lifetimes and no rotation fields.
            {"access_token": "a2", "expires_in": 600},
        ]
    )
    provider = TikTokProvider(
        Settings(tiktok_client_key="k", tiktok_client_secret="s"),
        httpx.Client(
            transport=httpx.MockTransport(lambda _r: httpx.Response(200, json=next(responses)))
        ),
    )
    before = datetime.now(UTC)
    first = provider.refresh(_expiring())
    assert first.expires_at is not None
    assert timedelta(seconds=7190) < first.expires_at - before < timedelta(seconds=7210)
    refresh_end = datetime.fromisoformat(first.extra["refresh_expires_at"])
    assert timedelta(days=179.9) < refresh_end - before < timedelta(days=180.1)

    second = provider.refresh(first)
    assert second.expires_at is not None
    assert second.expires_at - datetime.now(UTC) < timedelta(seconds=601)
    # Omitted fields keep what the previous response said, never a constant.
    assert second.refresh_token == "r2"
    assert second.extra["refresh_expires_at"] == first.extra["refresh_expires_at"]
    assert second.extra["open_id"] == "open-1"


# ------------------------------------------------------------ serialization (C3)


class RotatingTokenEndpoint:
    """A fake TikTok token endpoint that rotates the refresh token on every use."""

    def __init__(self) -> None:
        self.calls = 0
        self.current_refresh = "refresh-1"
        self._lock = threading.Lock()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        form = dict(httpx.QueryParams(request.content.decode()))
        with self._lock:
            self.calls += 1
            presented = form.get("refresh_token")
            if presented != self.current_refresh:
                return httpx.Response(
                    400, json={"error": "invalid_grant", "error_description": "token reused"}
                )
            number = self.calls + 1
            self.current_refresh = f"refresh-{number}"
        # Hold the request open so a second caller would overlap without a lock.
        time.sleep(0.4)
        return httpx.Response(
            200,
            json={
                "access_token": f"access-{number}",
                "refresh_token": f"refresh-{number}",
                "expires_in": 7200,
                "refresh_expires_in": 15552000,
                "open_id": "open-1",
            },
        )


def test_concurrent_refreshes_of_a_rotating_token_make_one_call(live) -> None:
    settings, database = live
    endpoint = RotatingTokenEndpoint()
    seed = ProviderRegistry(database, settings)
    account_id = _account(database, seed, "tiktok", _expiring())
    token_ref = str(_account_row(database, account_id).token_ref)

    # Separate registries and token stores, as two processes would have.
    registries = []
    for _ in range(2):
        registry = ProviderRegistry(database, settings)
        registry.set_provider(
            Platform.tiktok,
            TikTokProvider(settings, httpx.Client(transport=httpx.MockTransport(endpoint))),
        )
        registries.append(registry)

    results: list[TokenSet] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(2)

    def refresh(registry: ProviderRegistry) -> None:
        barrier.wait()
        try:
            results.append(registry.fresh_token(Platform.tiktok, token_ref))
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=refresh, args=(registry,)) for registry in registries]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert errors == []
    assert endpoint.calls == 1
    assert [token.access_token for token in results] == ["access-2", "access-2"]
    stored = seed.token_store.load(token_ref)
    assert stored is not None
    assert stored.refresh_token == "refresh-2" == endpoint.current_refresh
    assert _account_row(database, account_id).status == "connected"


HOLD_LOCK = r"""
import sys, time
from pathlib import Path
from runway.platforms.tokens import TokenStore
store = TokenStore(Path(sys.argv[1]))
with store.refresh_lock(sys.argv[2]):
    Path(sys.argv[3]).write_text("held")
    time.sleep(1.5)
"""


def test_refresh_lock_excludes_another_process(tmp_path: Path) -> None:
    secrets_dir = tmp_path / "secrets"
    held = tmp_path / "held.flag"
    child = subprocess.Popen(
        [sys.executable, "-c", HOLD_LOCK, str(secrets_dir), "abc123", str(held)],
    )
    try:
        deadline = time.monotonic() + 60
        while not held.exists():
            assert child.poll() is None, "lock holder exited early"
            assert time.monotonic() < deadline, "lock holder never acquired the lock"
            time.sleep(0.05)
        store = TokenStore(secrets_dir)
        with pytest.raises(TokenRefreshBusy), store.refresh_lock("abc123", timeout=0.2):
            pass
        # Other accounts are not blocked by this account's refresh.
        with store.refresh_lock("other-account", timeout=0.2):
            pass
        waited_from = time.monotonic()
        with store.refresh_lock("abc123", timeout=30):
            # Acquired only once the other process let go (it holds for 1.5 s).
            assert time.monotonic() - waited_from > 0.5
    finally:
        child.wait(timeout=30)


def test_refresh_lock_rejects_unsafe_token_refs(tmp_path: Path) -> None:
    store = TokenStore(tmp_path / "secrets")
    with pytest.raises(Exception, match="safe lock name"), store.refresh_lock("../escape"):
        pass


def test_a_busy_refresh_lock_is_retryable_not_a_reauth(live, monkeypatch) -> None:
    settings, database = live
    registry = ProviderRegistry(database, settings)
    account_id = _account(database, registry, "tiktok", _expiring())
    token_ref = str(_account_row(database, account_id).token_ref)
    with registry.token_store.refresh_lock(token_ref):
        other = ProviderRegistry(database, settings)
        original = other.token_store.refresh_lock

        def short_lock(ref: str, *, timeout: float = 0.2):  # type: ignore[no-untyped-def]
            return original(ref, timeout=0.2)

        monkeypatch.setattr(other.token_store, "refresh_lock", short_lock)
        with pytest.raises(ProviderError) as raised:
            other.fresh_token(Platform.tiktok, token_ref)
    assert not isinstance(raised.value, AuthorizationExpired)
    assert raised.value.retryable is True
