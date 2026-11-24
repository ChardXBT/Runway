"""`runway doctor platforms` report branches (fake settings and token stores).

The doctor answers one question per platform: "can the owner connect (or keep)
this account?" - credentials set and from where, the exact redirect URI the
OAuth code sends, the platform's HTTPS rule, scopes granted versus needed,
token state, app-status gates and the per-post-type capability. It never
prints secrets and makes no network call; `--check-edge` is the only probe and
goes to the owner's own public edge through an injected client in tests.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

import runway.config.instance as instance_config
from runway.cli.main import app
from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import ConnectedAccount
from runway.platforms.base import TokenSet
from runway.platforms.capabilities import (
    INSTAGRAM_SCOPES,
    TIKTOK_SCOPES,
    YOUTUBE_ANALYTICS_SCOPE,
    YOUTUBE_READONLY_SCOPE,
)
from runway.platforms.doctor import PlatformsDoctor
from runway.platforms.publish_capability import (
    INSTAGRAM_PUBLISH_SCOPE,
    YOUTUBE_UPLOAD_SCOPE,
)
from runway.platforms.tokens import TokenStore

NOW = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
IMPLEMENTED = frozenset(
    {
        "assisted",
        "youtube_direct",
        "youtube_post",
        "instagram_direct",
        "instagram_unified",
        "tiktok_direct",
        "tiktok_inbox",
    }
)

_CREDENTIAL_ENV = (
    "RUNWAY_YOUTUBE_CLIENT_ID",
    "RUNWAY_YOUTUBE_CLIENT_SECRET",
    "RUNWAY_INSTAGRAM_APP_ID",
    "RUNWAY_INSTAGRAM_APP_SECRET",
    "RUNWAY_META_APP_ID",
    "RUNWAY_META_APP_SECRET",
    "RUNWAY_INSTAGRAM_UNIFIED_CONFIG_ID",
    "RUNWAY_INSTAGRAM_WEBHOOK_VERIFY_TOKEN",
    "RUNWAY_TIKTOK_CLIENT_KEY",
    "RUNWAY_TIKTOK_CLIENT_SECRET",
    "RUNWAY_PUBLIC_EDGE_URL",
    "RUNWAY_OAUTH_REDIRECT_BASE_URL",
    "RUNWAY_TOKEN_KEY",
)


@pytest.fixture(autouse=True)
def _clean_credential_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)


def _settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {"data_dir": tmp_path / "data", "platform_provider": "live"}
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _doctor(
    database: Database,
    settings: Settings,
    *,
    implemented: frozenset[str] = IMPLEMENTED,
    **kwargs: object,
) -> PlatformsDoctor:
    kwargs.setdefault("clock", lambda: NOW)
    return PlatformsDoctor(database, settings, implemented=implemented, **kwargs)  # type: ignore[arg-type]


def _connect(
    database: Database,
    platform: str = "youtube",
    *,
    status: str = "connected",
    scopes: Sequence[str] = (),
    handle: str = "ownerchan",
    token_ref: str | None = "ref-1",
    metadata: dict[str, object] | None = None,
) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform=platform,
            external_account_id=f"ext-{platform}-1",
            handle=handle,
            status=status,
            granted_scopes_json=json.dumps(list(scopes)),
            token_ref=token_ref,
            metadata_json=json.dumps(metadata or {}),
        )
        session.add(account)
        session.flush()
        return account.id


def _store_token(settings: Settings, token_ref: str, token: TokenSet) -> None:
    TokenStore(settings.token_store_dir).store(token_ref, token)


def _platform(report: object, name: str) -> dict[str, object]:
    for view in report.platforms:  # type: ignore[attr-defined]
        if view.platform == name:
            return view.model_dump(mode="json")  # type: ignore[no-any-return]
    raise AssertionError(f"platform {name} missing from report")


def _configured_all(tmp_path: Path) -> Settings:
    """A fully configured owner install: every credential, https edge, all set."""
    return _settings(
        tmp_path,
        youtube_client_id="yt-id",
        youtube_client_secret="yt-secret",
        instagram_app_id="ig-id",
        instagram_app_secret="ig-secret",
        tiktok_client_key="tt-key",
        tiktok_client_secret="tt-secret",
        oauth_redirect_base_url="https://runway.example.ts.net",
        public_edge_url="https://runway.example.ts.net",
    )


def _connect_all(database: Database, settings: Settings) -> None:
    """One connected account per platform with a valid stored token."""
    base_scopes = {
        "youtube": [YOUTUBE_READONLY_SCOPE, YOUTUBE_ANALYTICS_SCOPE],
        "instagram": list(INSTAGRAM_SCOPES),
        "tiktok": list(TIKTOK_SCOPES),
    }
    for platform in ("youtube", "instagram", "tiktok"):
        _connect(database, platform, scopes=base_scopes[platform], token_ref=f"ref-{platform}")
        _store_token(
            settings,
            f"ref-{platform}",
            TokenSet(
                access_token="stored",
                refresh_token="refresh",
                expires_at=NOW + timedelta(hours=1),
                scopes=base_scopes[platform],
            ),
        )


# ---------------------------------------------------------------- credentials


def test_unconfigured_platform_reports_each_missing_credential(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    report = _doctor(database, settings).run()

    youtube = _platform(report, "youtube")
    assert youtube["configured"] is False
    by_name = {item["name"]: item for item in youtube["credentials"]}
    assert by_name["RUNWAY_YOUTUBE_CLIENT_ID"]["set"] is False
    assert by_name["RUNWAY_YOUTUBE_CLIENT_SECRET"]["set"] is False
    assert report.ready is False
    assert any("RUNWAY_YOUTUBE_CLIENT_ID" in item for item in report.missing)
    assert any("RUNWAY_YOUTUBE_CLIENT_SECRET" in item for item in report.missing)


def test_credential_source_is_the_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RUNWAY_YOUTUBE_CLIENT_ID", "env-id")
    monkeypatch.setenv("RUNWAY_YOUTUBE_CLIENT_SECRET", "env-secret")
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    youtube = _platform(_doctor(database, settings).run(), "youtube")

    by_name = {item["name"]: item for item in youtube["credentials"]}
    assert by_name["RUNWAY_YOUTUBE_CLIENT_ID"]["set"] is True
    assert by_name["RUNWAY_YOUTUBE_CLIENT_ID"]["source"] == "environment"
    assert youtube["configured"] is True


def test_credential_source_is_the_dotenv_file(tmp_path: Path) -> None:
    dotenv = tmp_path / ".env"
    dotenv.write_text(
        "RUNWAY_YOUTUBE_CLIENT_ID=dotenv-id\nRUNWAY_YOUTUBE_CLIENT_SECRET=dotenv-secret\n",
        encoding="utf-8",
    )
    settings = _settings(
        tmp_path, youtube_client_id="dotenv-id", youtube_client_secret="dotenv-secret"
    )
    database = initialize_database(settings)
    youtube = _platform(_doctor(database, settings, dotenv_path=dotenv).run(), "youtube")

    by_name = {item["name"]: item for item in youtube["credentials"]}
    assert by_name["RUNWAY_YOUTUBE_CLIENT_ID"]["source"] == ".env"
    assert by_name["RUNWAY_YOUTUBE_CLIENT_SECRET"]["source"] == ".env"


def test_credential_source_is_the_instance_secrets_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir()
    secrets_text = "RUNWAY_YOUTUBE_CLIENT_ID=from-secrets\nRUNWAY_YOUTUBE_CLIENT_SECRET=secret\n"
    (instance_dir / "secrets.enc.env").write_text("sops ciphertext", encoding="utf-8")

    def fake_sops(args: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(list(args), 0, stdout=secrets_text, stderr="")

    monkeypatch.setattr(instance_config, "sops_runner", fake_sops)
    settings = _settings(tmp_path, instance_dir=instance_dir)
    assert settings.youtube_client_id == "from-secrets"  # sanity: the layer loaded
    database = initialize_database(settings)
    youtube = _platform(_doctor(database, settings).run(), "youtube")

    by_name = {item["name"]: item for item in youtube["credentials"]}
    assert by_name["RUNWAY_YOUTUBE_CLIENT_ID"]["source"] == "secrets.enc.env"
    assert by_name["RUNWAY_YOUTUBE_CLIENT_SECRET"]["source"] == "secrets.enc.env"


def test_a_secret_value_never_appears_in_the_report(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        youtube_client_id="visible-id-123",
        youtube_client_secret="hidden-secret-456",
    )
    database = initialize_database(settings)
    report = _doctor(database, settings).run()

    rendered = json.dumps(report.as_dict()) + report.human_text()
    assert "hidden-secret-456" not in rendered


def test_the_webhook_verify_token_is_a_reported_credential(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    instagram = _platform(_doctor(database, settings).run(), "instagram")
    by_name = {item["name"]: item for item in instagram["credentials"]}

    view = by_name["RUNWAY_INSTAGRAM_WEBHOOK_VERIFY_TOKEN"]
    assert view["set"] is False
    assert view["adapter"] == "webhooks"

    monkeypatch.setenv("RUNWAY_INSTAGRAM_WEBHOOK_VERIFY_TOKEN", "chosen-value")
    settings = _settings(tmp_path / "other")
    database = initialize_database(settings)
    instagram = _platform(_doctor(database, settings).run(), "instagram")
    by_name = {item["name"]: item for item in instagram["credentials"]}

    view = by_name["RUNWAY_INSTAGRAM_WEBHOOK_VERIFY_TOKEN"]
    assert view["set"] is True
    assert view["source"] == "environment"
    # Set/unset only: it never counts as OAuth configuration.
    assert instagram["configured"] is False


def test_human_output_lists_the_publishing_detail_flags(tmp_path: Path) -> None:
    settings = _settings(tmp_path, inbox_replies_enabled=True)
    database = initialize_database(settings)
    report = _doctor(database, settings).run()

    text = report.human_text()
    assert "inbox replies on" in text
    assert "R2 media host not configured" in text

    settings = _settings(
        tmp_path / "r2",
        r2_account_id="acct",
        r2_bucket="media",
        r2_access_key_id="key",
        r2_secret_access_key="secret",
    )
    database = initialize_database(settings)
    report = _doctor(database, settings).run()

    assert "inbox replies off" in report.human_text()
    assert "R2 media host configured" in report.human_text()


# ---------------------------------------------------------------- redirect URIs


def test_youtube_redirect_allows_loopback_http(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    youtube = _platform(_doctor(database, settings).run(), "youtube")

    assert youtube["redirect_uri"].endswith("/api/platforms/youtube/oauth/callback")
    assert youtube["redirect_uri"].startswith("http://127.0.0.1:")
    assert youtube["redirect_https_required"] is False
    assert youtube["redirect_ok"] is True


def test_youtube_redirect_on_a_plain_http_host_is_flagged(tmp_path: Path) -> None:
    settings = _settings(tmp_path, oauth_redirect_base_url="http://192.168.1.10:8000")
    database = initialize_database(settings)
    youtube = _platform(_doctor(database, settings).run(), "youtube")

    assert youtube["redirect_ok"] is False
    assert any("RUNWAY_OAUTH_REDIRECT_BASE_URL" in item for item in youtube["missing"])


def test_meta_and_tiktok_redirects_require_https(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    report = _doctor(database, settings).run()

    for name in ("instagram", "tiktok"):
        view = _platform(report, name)
        assert view["redirect_https_required"] is True
        assert view["redirect_ok"] is False


def test_https_redirect_passes_every_platform(tmp_path: Path) -> None:
    settings = _settings(tmp_path, oauth_redirect_base_url="https://runway.example.ts.net")
    database = initialize_database(settings)
    report = _doctor(database, settings).run()

    for view in report.platforms:
        assert view.redirect_ok is True, view.platform
        assert view.redirect_uri.startswith("https://runway.example.ts.net/api/platforms/")


# ---------------------------------------------------------------- public edge


def test_tiktok_reports_the_missing_public_edge(tmp_path: Path) -> None:
    settings = _settings(
        tmp_path,
        tiktok_client_key="k",
        tiktok_client_secret="s",
        oauth_redirect_base_url="https://runway.example.ts.net",
    )
    database = initialize_database(settings)
    report = _doctor(database, settings).run()

    assert report.edge.set is False
    tiktok = _platform(report, "tiktok")
    assert any("RUNWAY_PUBLIC_EDGE_URL" in item for item in tiktok["missing"])


def test_check_edge_is_the_only_probe_and_uses_the_injected_client(tmp_path: Path) -> None:
    settings = _configured_all(tmp_path)
    database = initialize_database(settings)
    calls: list[str] = []

    def probe(url: str) -> tuple[bool, str]:
        calls.append(url)
        return True, "HTTP 200"

    report = _doctor(database, settings, edge_probe=probe).run(check_edge=True)

    assert calls == ["https://runway.example.ts.net/hooks/health"]
    assert report.edge.checked is True
    assert report.edge.reachable is True


def test_no_probe_runs_without_check_edge(tmp_path: Path) -> None:
    settings = _configured_all(tmp_path)
    database = initialize_database(settings)

    def probe(url: str) -> tuple[bool, str]:  # pragma: no cover - must not run
        raise AssertionError("the edge probe ran without --check-edge")

    report = _doctor(database, settings, edge_probe=probe).run()

    assert report.edge.checked is False
    assert report.edge.reachable is None


def test_a_failed_edge_check_blocks(tmp_path: Path) -> None:
    settings = _configured_all(tmp_path)
    database = initialize_database(settings)

    def probe(url: str) -> tuple[bool, str]:
        return False, "connection refused"

    report = _doctor(database, settings, edge_probe=probe).run(check_edge=True)

    assert report.edge.reachable is False
    assert report.ready is False
    assert any("edge" in item for item in report.missing)


# ---------------------------------------------------------------- accounts


def test_a_configured_but_unconnected_platform_is_missing_the_connect_step(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path, youtube_client_id="id", youtube_client_secret="secret")
    database = initialize_database(settings)
    youtube = _platform(_doctor(database, settings).run(), "youtube")

    assert youtube["ready"] is False
    assert any("connect" in item.lower() for item in youtube["missing"])


def test_fully_connected_install_is_ready(tmp_path: Path) -> None:
    settings = _configured_all(tmp_path)
    database = initialize_database(settings)
    _connect_all(database, settings)
    report = _doctor(database, settings).run()

    assert report.missing == []
    assert report.ready is True
    for view in report.platforms:
        assert view.ready is True, view.platform


def test_needs_reauth_and_disconnected_accounts_ask_for_reconnect(tmp_path: Path) -> None:
    settings = _configured_all(tmp_path)
    database = initialize_database(settings)
    _connect_all(database, settings)
    with database.session() as session:
        account = session.get(ConnectedAccount, 1)
        assert account is not None
        account.status = "needs_reauth"
    report = _doctor(database, settings).run()

    assert report.ready is False
    assert any("reconnect" in item.lower() for item in report.missing)


def test_token_states(tmp_path: Path) -> None:
    settings = _configured_all(tmp_path)
    database = initialize_database(settings)

    # Expired access token but a refresh token exists: sync refreshes it.
    _connect(database, "youtube", scopes=[YOUTUBE_READONLY_SCOPE], token_ref="ref-youtube")
    _store_token(
        settings,
        "ref-youtube",
        TokenSet(
            access_token="a",
            refresh_token="r",
            expires_at=NOW - timedelta(minutes=5),
            scopes=[YOUTUBE_READONLY_SCOPE],
        ),
    )
    # Expired and no way to refresh: the owner must reconnect.
    _connect(
        database,
        "instagram",
        scopes=list(INSTAGRAM_SCOPES),
        token_ref="ref-instagram",
    )
    _store_token(
        settings,
        "ref-instagram",
        TokenSet(
            access_token="a",
            expires_at=NOW - timedelta(days=1),
            scopes=list(INSTAGRAM_SCOPES),
        ),
    )
    report = _doctor(database, settings).run()

    youtube_token = _platform(report, "youtube")["accounts"][0]["token"]
    assert youtube_token["state"] == "refreshable"
    instagram_token = _platform(report, "instagram")["accounts"][0]["token"]
    assert instagram_token["state"] == "expired"
    assert any("instagram" in item for item in report.missing)


def test_tiktok_refresh_expiry_is_reported(tmp_path: Path) -> None:
    settings = _configured_all(tmp_path)
    database = initialize_database(settings)
    _connect(database, "tiktok", scopes=list(TIKTOK_SCOPES), token_ref="ref-tiktok")
    _store_token(
        settings,
        "ref-tiktok",
        TokenSet(
            access_token="a",
            refresh_token="r",
            expires_at=NOW + timedelta(hours=1),
            scopes=list(TIKTOK_SCOPES),
            extra={"refresh_expires_at": (NOW - timedelta(days=1)).isoformat()},
        ),
    )
    tiktok = _platform(_doctor(database, settings).run(), "tiktok")

    assert tiktok["accounts"][0]["token"]["state"] == "refresh_expired"
    assert any("tiktok" in item for item in tiktok["missing"])


def test_an_account_without_a_stored_token_needs_reconnect(tmp_path: Path) -> None:
    settings = _configured_all(tmp_path)
    database = initialize_database(settings)
    _connect(database, "youtube", token_ref="ref-youtube")  # nothing stored
    youtube = _platform(_doctor(database, settings).run(), "youtube")

    assert youtube["accounts"][0]["token"]["present"] is False
    assert any("reconnect" in item.lower() for item in youtube["missing"])


def _files_under(directory: Path) -> set[Path]:
    return {path.relative_to(directory) for path in directory.rglob("*") if path.is_file()}


def test_the_doctor_writes_no_file_under_data(tmp_path: Path) -> None:
    """The doctor is read-only: no token.key, no secrets dir, no new files."""
    settings = _configured_all(tmp_path)
    database = initialize_database(settings)
    _connect_all(database, settings)
    data_dir = settings.resolved_data_dir
    before = _files_under(data_dir)

    # The default constructor path (real publisher registry included).
    report = PlatformsDoctor(database, settings, clock=lambda: NOW).run()

    assert _files_under(data_dir) == before
    assert report.ready is True


def test_a_fresh_install_reports_missing_without_creating_the_secrets_dir(
    tmp_path: Path,
) -> None:
    """No token.key and no store: the token is missing - and still nothing is written."""
    settings = _configured_all(tmp_path)
    database = initialize_database(settings)
    _connect(database, "youtube", token_ref="ref-youtube")

    report = _doctor(database, settings).run()

    token = _platform(report, "youtube")["accounts"][0]["token"]
    assert token["state"] == "missing"
    assert not settings.token_store_dir.exists()


def test_a_missing_token_key_reports_unreadable_and_writes_nothing(
    tmp_path: Path,
) -> None:
    """A store blob but no token.key: unreadable with the reason - no rewrite."""
    settings = _configured_all(tmp_path)
    database = initialize_database(settings)
    _connect(
        database,
        "youtube",
        scopes=[YOUTUBE_READONLY_SCOPE],
        token_ref="ref-youtube",
    )
    _store_token(
        settings,
        "ref-youtube",
        TokenSet(access_token="a", scopes=[YOUTUBE_READONLY_SCOPE]),
    )
    (settings.token_store_dir / "token.key").unlink()  # the key is gone, the blob stays
    data_dir = settings.resolved_data_dir
    before = _files_under(data_dir)

    report = _doctor(database, settings).run()

    assert _files_under(data_dir) == before
    token = _platform(report, "youtube")["accounts"][0]["token"]
    assert token["state"] == "unreadable"
    assert "token.key" in token["reason"]
    assert any("unreadable" in item for item in report.missing)


def test_an_undecryptable_store_reports_unreadable_with_reason(tmp_path: Path) -> None:
    settings = _configured_all(tmp_path)
    database = initialize_database(settings)
    _connect(
        database,
        "youtube",
        scopes=[YOUTUBE_READONLY_SCOPE],
        token_ref="ref-youtube",
    )
    _store_token(
        settings,
        "ref-youtube",
        TokenSet(access_token="a", scopes=[YOUTUBE_READONLY_SCOPE]),
    )
    store = TokenStore(settings.token_store_dir)
    store.key_path.write_bytes(Fernet.generate_key() + b"\n")  # rotate the key away

    youtube = _platform(_doctor(database, settings).run(), "youtube")

    token = youtube["accounts"][0]["token"]
    assert token["state"] == "unreadable"
    assert token["reason"]
    assert "token unreadable" in _doctor(database, settings).run().human_text()


# --------------------------------------------------------------------- scopes


def test_scopes_reported_are_requested_minus_granted(tmp_path: Path) -> None:
    settings = _settings(tmp_path, youtube_client_id="id", youtube_client_secret="s")
    database = initialize_database(settings)
    # The owner granted only the analytics scope: readonly is missing.
    _connect(
        database,
        "youtube",
        scopes=[YOUTUBE_ANALYTICS_SCOPE],
        token_ref="ref-youtube",
    )
    _store_token(
        settings,
        "ref-youtube",
        TokenSet(access_token="a", refresh_token="r", scopes=[YOUTUBE_ANALYTICS_SCOPE]),
    )
    youtube = _platform(_doctor(database, settings).run(), "youtube")
    account = youtube["accounts"][0]

    assert YOUTUBE_READONLY_SCOPE in account["missing_connect_scopes"]
    assert YOUTUBE_ANALYTICS_SCOPE not in account["missing_connect_scopes"]
    assert YOUTUBE_UPLOAD_SCOPE in account["missing_publishing_scopes"]


def test_unified_login_scopes_satisfy_the_instagram_requirement(tmp_path: Path) -> None:
    settings = _configured_all(tmp_path)
    database = initialize_database(settings)
    _connect(
        database,
        "instagram",
        scopes=["instagram_basic", "instagram_content_publish"],
        token_ref="ref-instagram",
        metadata={"connection": "instagram_unified"},
    )
    _store_token(
        settings,
        "ref-instagram",
        TokenSet(access_token="a", scopes=["instagram_basic", "instagram_content_publish"]),
    )
    instagram = _platform(_doctor(database, settings).run(), "instagram")
    account = instagram["accounts"][0]

    assert INSTAGRAM_PUBLISH_SCOPE not in account["missing_publishing_scopes"]
    publishing = {item["post_type"]: item["state"] for item in account["publishing"]}
    # No R2 in this install: a Reel through Unified Login is ready (upload from
    # disk); an image post still needs the media host.
    assert publishing["instagram_reel"] == "ready"
    assert publishing["instagram_image"] == "needs_media_host"


def test_capability_per_post_type_carries_the_reason(tmp_path: Path) -> None:
    settings = _configured_all(tmp_path)
    database = initialize_database(settings)
    _connect(
        database,
        "youtube",
        scopes=[YOUTUBE_READONLY_SCOPE, YOUTUBE_ANALYTICS_SCOPE],
        token_ref="ref-youtube",
    )
    _store_token(
        settings,
        "ref-youtube",
        TokenSet(
            access_token="a",
            refresh_token="r",
            scopes=[YOUTUBE_READONLY_SCOPE, YOUTUBE_ANALYTICS_SCOPE],
        ),
    )
    youtube = _platform(_doctor(database, settings).run(), "youtube")
    account = youtube["accounts"][0]
    states = {item["post_type"]: item["state"] for item in account["publishing"]}

    assert states["youtube_video"] == "needs_scope"
    # The posts API is a pilot: the reason is the missing app access, not scope.
    assert states["youtube_post"] == "needs_app_approval"


# ------------------------------------------------------------- instance state


def test_a_standby_machine_blocks_connecting(tmp_path: Path) -> None:
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir()
    settings = _settings(tmp_path, instance_dir=instance_dir)
    database = initialize_database(settings)
    report = _doctor(database, settings, is_active=lambda _s: False).run()

    assert report.instance.configured is True
    assert report.instance.state == "standby"
    assert any("standby" in item for item in report.missing)


def test_an_active_instance_is_reported(tmp_path: Path) -> None:
    instance_dir = tmp_path / "instance"
    instance_dir.mkdir()
    settings = _settings(tmp_path, instance_dir=instance_dir)
    database = initialize_database(settings)
    report = _doctor(database, settings, is_active=lambda _s: True).run()

    assert report.instance.configured is True
    assert report.instance.state == "active"


def test_without_an_instance_the_state_is_not_configured(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    database = initialize_database(settings)
    report = _doctor(database, settings).run()

    assert report.instance.configured is False
    assert report.instance.state == "not_configured"


# ------------------------------------------------------------------ mock mode


def test_mock_mode_is_ready_without_credentials(tmp_path: Path) -> None:
    settings = _settings(tmp_path, platform_provider="mock")
    database = initialize_database(settings)
    report = _doctor(database, settings).run()

    assert report.provider_mode == "mock"
    assert report.ready is True
    assert any("mock" in note for view in report.platforms for note in view.notes)


# ------------------------------------------------------------------ CLI + API


def test_cli_platforms_json_exit_codes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import runway.cli.main as cli_main

    settings = _configured_all(tmp_path)
    database = initialize_database(settings)
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    monkeypatch.setattr(cli_main, "initialize_database", lambda _s: database)

    runner = CliRunner()
    missing = runner.invoke(app, ["doctor", "platforms", "--json"])
    assert missing.exit_code == 1
    payload = json.loads(missing.stdout)
    assert payload["ready"] is False
    assert payload["missing"]

    _connect_all(database, settings)
    ready = runner.invoke(app, ["doctor", "platforms", "--json"])
    assert ready.exit_code == 0
    assert json.loads(ready.stdout)["ready"] is True


def test_cli_platforms_human_output_lists_missing_steps(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import runway.cli.main as cli_main

    settings = _settings(tmp_path)
    database = initialize_database(settings)
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    monkeypatch.setattr(cli_main, "initialize_database", lambda _s: database)

    result = CliRunner().invoke(app, ["doctor", "platforms"])
    assert result.exit_code == 1
    assert "What is missing" in result.stdout
    assert "RUNWAY_YOUTUBE_CLIENT_ID" in result.stdout
    assert "oauth/callback" in result.stdout


def test_doctor_group_keeps_the_environment_check_and_adds_platforms() -> None:
    result = CliRunner().invoke(app, ["doctor", "--help"])
    assert result.exit_code == 0
    # The bare `runway doctor` environment check lives in the group's callback.
    assert "platforms" in result.stdout


def test_bare_doctor_still_runs_the_environment_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import runway.cli.main as cli_main

    settings = _settings(tmp_path)
    database = initialize_database(settings)
    monkeypatch.setattr(cli_main, "get_settings", lambda: settings)
    monkeypatch.setattr(cli_main, "initialize_database", lambda _s: database)

    result = CliRunner().invoke(app, ["doctor"])

    # Whatever the individual verdicts are, the environment check itself ran:
    # it prints one OK/WARN/FAIL line per probe.
    for probe in ("Python", "Node", "npm", "Database migration", "API port"):
        assert probe in result.stdout


def test_v1_doctor_route_returns_the_report(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from runway.api.app import create_app

    settings = _settings(tmp_path, platform_provider="mock")
    initialize_database(settings)
    client = TestClient(create_app(settings), base_url="http://127.0.0.1:8000")

    response = client.get("/api/v1/doctor/platforms")
    assert response.status_code == 200
    payload = response.json()
    assert payload["ready"] is True
    assert {view["platform"] for view in payload["platforms"]} == {
        "youtube",
        "instagram",
        "tiktok",
    }
