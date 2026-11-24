"""The ``runway doctor platforms`` report: is each platform ready to connect?

Per platform it answers, in plain words and without ever printing a secret:

- the credential settings (set? read from where - the environment, the
  ``.env`` file, ``instance.toml`` or ``secrets.enc.env`` - never the value),
- the exact redirect URI the OAuth code sends, taken from
  ``ProviderRegistry.redirect_uri`` so the doctor can never drift from the
  callback the app really uses,
- whether that URI passes the platform's HTTPS rule (Meta and TikTok require
  https; Google additionally allows ``http://localhost`` and
  ``http://127.0.0.1`` loopbacks for installed apps),
- the scopes the connect consent asks for versus the scopes each stored
  account already holds, and the scopes enabling publishing adds,
- the stored token's state (expires, refresh expires, needs reauth),
- the app-status settings that gate publishing (Meta app mode, TikTok audit,
  YouTube verification and the posts-API pilot), and
- the publishing capability per post type with its reason.

The ``instance`` section says whether this private instance is the active
machine - connecting and storing tokens are active-machine writes - and
whether credentials really come from ``secrets.enc.env``. The ``edge``
section shows ``RUNWAY_PUBLIC_EDGE_URL``, which TikTok OAuth and the platform
webhooks need, and optionally probes ``{edge}/hooks/health``.

Everything ends in ``missing``: the one-line "what to do next" actions. The
command exits 1 while that list is not empty.

No network call happens by default. ``--check-edge`` is the only probe and it
asks the owner's own public edge, never a platform; tests inject it.
"""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel
from sqlalchemy import select

from runway.config import Settings
from runway.config.instance import (
    INSTANCE_TOML_NAME,
    SECRETS_FILE_FIELDS,
    SECRETS_FILE_NAME,
)
from runway.db.base import Database
from runway.db.models import ConnectedAccount
from runway.operations.instance_lease import instance_is_active
from runway.platforms.base import TokenSet, iso_utc, safe_error_summary
from runway.platforms.capabilities import (
    INSTAGRAM_SCOPES,
    TIKTOK_SCOPES,
    YOUTUBE_ANALYTICS_SCOPE,
    YOUTUBE_ENGAGE_SCOPE,
    YOUTUBE_READONLY_SCOPE,
    Platform,
    equivalent_scopes,
)
from runway.platforms.publish_capability import (
    INSTAGRAM_PUBLISH_SCOPE,
    TIKTOK_PUBLISH_SCOPE,
    TIKTOK_UPLOAD_SCOPE,
    YOUTUBE_UPLOAD_SCOPE,
    AccountFacts,
    AppStatus,
    account_publishing_capabilities,
)
from runway.platforms.publisher_registry import build_publisher_registry
from runway.platforms.registry import ProviderRegistry
from runway.platforms.tokens import TokenStoreError

# Meta and TikTok reject plain-http redirect URIs; Google's installed-app
# OAuth still allows loopback http (localhost, 127.0.0.1, ::1).
_HTTPS_REDIRECT_PLATFORMS = frozenset({Platform.instagram, Platform.tiktok})
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})

EdgeProbe = Callable[[str], tuple[bool, str]]
"""``(url) -> (ok, detail)``; the only network call this module can make."""


@dataclass(frozen=True)
class _Credential:
    """One credential setting: its field name, its RUNWAY_* name, its use."""

    field: str
    env_name: str
    label: str
    # Which connect path uses it: "" for the platform's own OAuth app,
    # "instagram" for Instagram Login, "instagram_unified" for Unified Login.
    adapter: str = ""


_PLATFORM_CREDENTIALS: dict[Platform, tuple[_Credential, ...]] = {
    Platform.youtube: (
        _Credential("youtube_client_id", "RUNWAY_YOUTUBE_CLIENT_ID", "client id"),
        _Credential("youtube_client_secret", "RUNWAY_YOUTUBE_CLIENT_SECRET", "client secret"),
    ),
    Platform.instagram: (
        _Credential("instagram_app_id", "RUNWAY_INSTAGRAM_APP_ID", "app id", "instagram"),
        _Credential(
            "instagram_app_secret",
            "RUNWAY_INSTAGRAM_APP_SECRET",
            "app secret",
            "instagram",
        ),
        _Credential("meta_app_id", "RUNWAY_META_APP_ID", "Meta app id", "instagram_unified"),
        _Credential(
            "meta_app_secret",
            "RUNWAY_META_APP_SECRET",
            "Meta app secret",
            "instagram_unified",
        ),
        _Credential(
            "instagram_unified_config_id",
            "RUNWAY_INSTAGRAM_UNIFIED_CONFIG_ID",
            "config id",
            "instagram_unified",
        ),
    ),
    Platform.tiktok: (
        _Credential("tiktok_client_key", "RUNWAY_TIKTOK_CLIENT_KEY", "client key"),
        _Credential("tiktok_client_secret", "RUNWAY_TIKTOK_CLIENT_SECRET", "client secret"),
    ),
}

_APP_STATUS_SETTINGS: dict[Platform, tuple[str, str]] = {
    Platform.youtube: ("youtube_app_status", "RUNWAY_YOUTUBE_APP_STATUS"),
    Platform.instagram: ("instagram_app_status", "RUNWAY_INSTAGRAM_APP_STATUS"),
    Platform.tiktok: ("tiktok_app_status", "RUNWAY_TIKTOK_APP_STATUS"),
}


# --------------------------------------------------------------------- report


class SettingView(BaseModel):
    """One credential setting: set or not, and the layer it came from."""

    name: str  # the RUNWAY_* environment name
    label: str
    adapter: str
    set: bool
    source: str | None  # "environment" | ".env" | "instance.toml" | "secrets.enc.env" | "settings"


class TokenView(BaseModel):
    """The stored token's state; secret material is never part of the report."""

    present: bool
    state: str  # missing | unreadable | valid | refreshable | expired | refresh_expired
    expires_at: str | None
    expired: bool
    refresh_token: bool
    refresh_expires_at: str | None


class AccountView(BaseModel):
    """One connected account: status, scopes, token and per-post-type gates."""

    id: int
    handle: str | None
    status: str
    connection: str
    granted_scopes: list[str]
    scopes_source: str | None
    missing_connect_scopes: list[str]
    missing_publishing_scopes: list[str]
    token: TokenView
    publishing: list[dict[str, object]]


class PlatformView(BaseModel):
    """Everything the doctor knows about one platform."""

    platform: str
    configured: bool
    credentials: list[SettingView]
    redirect_uri: str
    redirect_https_required: bool
    redirect_ok: bool
    connect_scopes: list[str]
    publishing_scopes: list[str]
    app_status: str
    app_status_setting: str
    accounts: list[AccountView]
    # The per-post-type preview without an account (shows the app gates).
    capabilities: list[dict[str, object]]
    notes: list[str]
    missing: list[str]
    ready: bool


class InstanceView(BaseModel):
    """Private-instance state: connecting is an active-machine write."""

    configured: bool
    state: str  # "not_configured" | "active" | "standby"
    secrets_file_present: bool
    secrets_in_use: bool


class EdgeView(BaseModel):
    """The public edge (Tailscale Funnel) TikTok and webhooks need."""

    url: str | None
    set: bool
    health_url: str | None
    checked: bool
    reachable: bool | None
    detail: str | None


class PublishingView(BaseModel):
    """The publishing gates that decide whether a "ready" account can post."""

    enabled: bool
    mode: str
    paused: bool
    youtube_automation_authorized: bool
    inbox_replies_enabled: bool
    r2_configured: bool


class PlatformsDoctorReport(BaseModel):
    """The full report; ``missing`` is the owner-facing to-do list."""

    generated_at: str
    profile: str
    provider_mode: str
    publishing: PublishingView
    app_status: dict[str, object]
    instance: InstanceView
    edge: EdgeView
    platforms: list[PlatformView]
    missing: list[str]
    ready: bool

    def as_dict(self) -> dict[str, object]:
        return self.model_dump(mode="json")

    def human_text(self) -> str:
        """Render the report for a terminal."""
        lines = [
            f"Runway platform doctor - {self.generated_at}",
            f"Profile: {self.profile}    Provider: {self.provider_mode}",
            (
                "Publishing: "
                f"{'enabled' if self.publishing.enabled else 'disabled'}"
                f" (mode {self.publishing.mode})"
                f"{'; paused' if self.publishing.paused else ''}"
                + (
                    "; YouTube automation authorized"
                    if self.publishing.youtube_automation_authorized
                    else ""
                )
            ),
            (
                "App status: "
                f"youtube={self.app_status['youtube']}, "
                f"instagram={self.app_status['instagram']}, "
                f"tiktok={self.app_status['tiktok']}; "
                f"YouTube posts API access: "
                f"{'yes' if self.app_status['youtube_posts_api_access'] else 'no'}"
            ),
            (
                "Private instance: "
                + (
                    {
                        "not_configured": "not configured - secrets come from .env / environment",
                        "active": "active - this machine holds the lease",
                        "standby": "standby - connect and publish on the machine holding the lease",
                    }[self.instance.state]
                )
                + ("; secrets.enc.env in use" if self.instance.secrets_in_use else "")
            ),
            (
                "Public edge: "
                + (self.edge.url or "not set - TikTok connect and webhooks need it")
                + (
                    f"; /hooks/health {self.edge.detail}"
                    if self.edge.checked and self.edge.detail
                    else ""
                )
            ),
            "",
        ]
        for view in self.platforms:
            lines.append(view.platform.upper())
            for item in view.credentials:
                state = f"set ({item.source})" if item.set else "MISSING"
                adapter = f" [{item.adapter}]" if item.adapter else ""
                lines.append(f"  {item.name:<45} {state}{adapter}")
            lines.append(f"  Redirect URI to register: {view.redirect_uri}")
            if not view.redirect_ok:
                reason = (
                    "https required - set RUNWAY_OAUTH_REDIRECT_BASE_URL to the public edge"
                    if view.redirect_https_required
                    else "plain http is allowed only on a loopback host"
                )
                lines.append(f"    redirect fails the platform rule: {reason}")
            lines.append(
                f"  Connect scopes: {len(view.connect_scopes)}; "
                f"publishing adds: {', '.join(view.publishing_scopes)}"
            )
            lines.append(f"  App status: {view.app_status} ({view.app_status_setting})")
            for account in view.accounts:
                token = account.token
                token_text = (
                    "no stored token"
                    if not token.present
                    else (
                        f"token {token.state}"
                        + (f" until {token.expires_at}" if token.expires_at else "")
                        + ("; refresh token held" if token.refresh_token else "")
                    )
                )
                lines.append(
                    f"  @{account.handle or account.connection} ({account.status}) - {token_text}"
                )
                if account.missing_connect_scopes:
                    lines.append(
                        f"    missing connect scopes: {', '.join(account.missing_connect_scopes)}"
                    )
                if account.missing_publishing_scopes:
                    lines.append(
                        "    publishing consent still needs: "
                        + ", ".join(account.missing_publishing_scopes)
                    )
                for capability in account.publishing:
                    lines.append(
                        f"    {capability['post_type']:<22} {capability['state']}"
                        f" - {capability['detail']}"
                    )
            for note in view.notes:
                lines.append(f"  note: {note}")
            lines.append(f"  {'READY' if view.ready else 'NOT READY'}")
            lines.append("")
        lines.append("What is missing:")
        if self.missing:
            lines.extend(f"  - {item}" for item in self.missing)
            lines.append(
                "Run `runway doctor platforms` again after each fix; exit code 1 until empty."
            )
        else:
            lines.append("  nothing - every platform can be connected.")
        return "\n".join(lines)


# --------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class _SettingSources:
    """Where a credential may have been read from. Names only - never values."""

    env_names: frozenset[str]
    dotenv_names: frozenset[str]
    instance_toml_fields: frozenset[str]
    secrets_file_present: bool


def _dotenv_setting_names(path: Path) -> frozenset[str]:
    """The *names* a .env file defines; the values are never kept."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return frozenset()
    names: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        stripped = stripped.removeprefix("export").lstrip()
        name, separator, _ = stripped.partition("=")
        if separator and name.strip():
            names.add(name.strip())
    return frozenset(names)


def _instance_toml_fields(instance_dir: Path | None) -> frozenset[str]:
    if instance_dir is None:
        return frozenset()
    try:
        payload = tomllib.loads((instance_dir / INSTANCE_TOML_NAME).read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return frozenset()
    return frozenset(key for key in payload if isinstance(key, str))


def _setting_sources(
    settings: Settings,
    environ: Mapping[str, str],
    dotenv_path: Path | None,
) -> _SettingSources:
    instance_dir = settings.resolved_instance_dir
    return _SettingSources(
        env_names=frozenset(
            name for name, value in environ.items() if name.startswith("RUNWAY_") and value.strip()
        ),
        dotenv_names=(_dotenv_setting_names(dotenv_path) if dotenv_path else frozenset()),
        instance_toml_fields=_instance_toml_fields(instance_dir),
        secrets_file_present=(
            instance_dir is not None and (instance_dir / SECRETS_FILE_NAME).is_file()
        ),
    )


def _credential_source(credential: _Credential, sources: _SettingSources) -> str:
    """The layer a set credential came from, in pydantic-settings priority order."""
    if credential.env_name in sources.env_names:
        return "environment"
    if credential.env_name in sources.dotenv_names:
        return ".env"
    if credential.field in sources.instance_toml_fields:
        return INSTANCE_TOML_NAME
    if sources.secrets_file_present and credential.field in SECRETS_FILE_FIELDS:
        return SECRETS_FILE_NAME
    return "settings"


def _redirect_ok(platform: Platform, redirect_uri: str) -> bool:
    """The platform's HTTPS rule applied to the URI the callback will send."""
    parsed = urlsplit(redirect_uri)
    if platform in _HTTPS_REDIRECT_PLATFORMS:
        return parsed.scheme == "https"
    if parsed.scheme == "https":
        return True
    # Google accepts http only on loopback hosts (installed-app flow).
    return parsed.scheme == "http" and (parsed.hostname or "") in _LOOPBACK_HOSTS


def _aware(moment: datetime) -> datetime:
    """Naive stored datetimes are UTC; compare everything aware."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def _parse_instant(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _token_view(token: TokenSet | None, now: datetime, *, unreadable: bool = False) -> TokenView:
    if unreadable:
        return TokenView(
            present=True,
            state="unreadable",
            expires_at=None,
            expired=True,
            refresh_token=False,
            refresh_expires_at=None,
        )
    if token is None:
        return TokenView(
            present=False,
            state="missing",
            expires_at=None,
            expired=False,
            refresh_token=False,
            refresh_expires_at=None,
        )
    refresh_expires_at = _parse_instant(token.extra.get("refresh_expires_at"))
    expired = token.expires_at is not None and _aware(token.expires_at) <= now
    if refresh_expires_at is not None and refresh_expires_at <= now:
        state = "refresh_expired"
    elif expired:
        # An expired access token with a refresh token is still healthy: sync
        # refreshes it on the next call. Only a dead grant needs the owner.
        state = "refreshable" if token.refresh_token else "expired"
    else:
        state = "valid"
    return TokenView(
        present=True,
        state=state,
        expires_at=token.expires_at.isoformat() if token.expires_at else None,
        expired=expired,
        refresh_token=bool(token.refresh_token),
        refresh_expires_at=refresh_expires_at.isoformat() if refresh_expires_at else None,
    )


def _connect_scopes(platform: Platform, settings: Settings) -> list[str]:
    """The scopes the OAuth start consent asks for (mirrors the providers)."""
    if platform is Platform.youtube:
        scopes = [YOUTUBE_READONLY_SCOPE, YOUTUBE_ANALYTICS_SCOPE]
        if settings.youtube_engage_scopes:
            scopes.append(YOUTUBE_ENGAGE_SCOPE)
        return scopes
    if platform is Platform.instagram:
        return list(INSTAGRAM_SCOPES)
    return list(TIKTOK_SCOPES)


def _publishing_scopes(platform: Platform, settings: Settings) -> list[str]:
    """The scopes the enable-publishing consent adds (mirrors accounts_routes)."""
    if platform is Platform.youtube:
        scopes = [YOUTUBE_UPLOAD_SCOPE]
        # youtube.force-ssl only when the build can use the posts API.
        if settings.youtube_posts_api_access:
            scopes.append(YOUTUBE_ENGAGE_SCOPE)
        return scopes
    if platform is Platform.instagram:
        return [INSTAGRAM_PUBLISH_SCOPE]
    scopes = [TIKTOK_UPLOAD_SCOPE]
    # `video.publish` is asked for only by an audited app (spec 08 section 4).
    if settings.tiktok_app_status == "audited":
        scopes.append(TIKTOK_PUBLISH_SCOPE)
    return scopes


def _default_edge_probe(url: str) -> tuple[bool, str]:
    """GET the edge health route - the only network call this module makes."""
    try:
        response = httpx.get(url, timeout=5.0)
    except httpx.HTTPError as exc:
        return False, safe_error_summary(exc)
    detail = f"HTTP {response.status_code}"
    try:
        payload = response.json()
    except ValueError:
        return False, f"{detail}; response was not JSON"
    if response.status_code == 200 and isinstance(payload, dict) and payload.get("ok") is True:
        return True, detail
    return False, detail


# --------------------------------------------------------------------- doctor


class PlatformsDoctor:
    """Build the connect-readiness report. Read-only; no writes, no network."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        *,
        implemented: Collection[str] | None = None,
        dotenv_path: Path | None = None,
        edge_probe: EdgeProbe | None = None,
        is_active: Callable[[Settings], bool] = instance_is_active,
        clock: Callable[[], datetime] | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.implemented = (
            frozenset(implemented)
            if implemented is not None
            else build_publisher_registry(settings, database).names()
        )
        self.dotenv_path = dotenv_path
        self.edge_probe = edge_probe or _default_edge_probe
        self.is_active = is_active
        self.clock = clock or (lambda: datetime.now(UTC))
        self.environ = environ if environ is not None else os.environ

    def run(self, *, check_edge: bool = False) -> PlatformsDoctorReport:
        settings = self.settings
        now = self.clock()
        registry = ProviderRegistry(self.database, settings)
        mock_mode = settings.platform_provider == "mock"
        sources = _setting_sources(settings, self.environ, self.dotenv_path)
        edge = self._edge_view(check_edge)
        platforms = [
            self._platform_view(platform, registry, sources, mock_mode, now)
            for platform in Platform
        ]
        instance = self._instance_view(sources, platforms)
        report_missing: list[str] = []
        if instance.state == "standby":
            report_missing.append(
                "instance: this machine is in standby; connecting is an "
                "active-machine write - connect on the machine holding the "
                "lease, or run `runway instance pull` here"
            )
        if edge.checked and edge.reachable is False:
            report_missing.append(
                f"edge: {edge.health_url} failed ({edge.detail}) - check the "
                "Tailscale Funnel/serve configuration"
            )
        for view in platforms:
            report_missing.extend(view.missing)
        return PlatformsDoctorReport(
            generated_at=iso_utc(now) or now.isoformat(),
            profile=settings.profile,
            provider_mode=settings.platform_provider,
            publishing=PublishingView(
                enabled=settings.publishing_enabled,
                mode=settings.publishing_mode,
                paused=settings.publishing_paused,
                youtube_automation_authorized=settings.youtube_automation_authorized,
                inbox_replies_enabled=settings.inbox_replies_enabled,
                r2_configured=settings.r2_configured,
            ),
            app_status={
                "youtube": settings.youtube_app_status,
                "instagram": settings.instagram_app_status,
                "tiktok": settings.tiktok_app_status,
                "youtube_posts_api_access": settings.youtube_posts_api_access,
            },
            instance=instance,
            edge=edge,
            platforms=platforms,
            missing=report_missing,
            ready=not report_missing,
        )

    # -------------------------------------------------------------- sections

    def _instance_view(
        self, sources: _SettingSources, platforms: list[PlatformView]
    ) -> InstanceView:
        configured = self.settings.resolved_instance_dir is not None
        state = "not_configured"
        if configured:
            state = "active" if self.is_active(self.settings) else "standby"
        return InstanceView(
            configured=configured,
            state=state,
            secrets_file_present=sources.secrets_file_present,
            secrets_in_use=any(
                item.source == SECRETS_FILE_NAME for view in platforms for item in view.credentials
            ),
        )

    def _edge_view(self, check_edge: bool) -> EdgeView:
        url = self.settings.public_edge_url or None
        health_url = f"{url}/hooks/health" if url else None
        if not (check_edge and health_url):
            return EdgeView(
                url=url,
                set=bool(url),
                health_url=health_url,
                checked=False,
                reachable=None,
                detail=None,
            )
        ok, detail = self.edge_probe(health_url)
        return EdgeView(
            url=url,
            set=True,
            health_url=health_url,
            checked=True,
            reachable=ok,
            detail=detail,
        )

    # ------------------------------------------------------------- platforms

    def _platform_view(
        self,
        platform: Platform,
        registry: ProviderRegistry,
        sources: _SettingSources,
        mock_mode: bool,
        now: datetime,
    ) -> PlatformView:
        settings = self.settings
        missing: list[str] = []
        notes: list[str] = []
        credentials = [
            SettingView(
                name=item.env_name,
                label=item.label,
                adapter=item.adapter,
                set=bool(getattr(settings, item.field)),
                source=(
                    _credential_source(item, sources) if getattr(settings, item.field) else None
                ),
            )
            for item in _PLATFORM_CREDENTIALS[platform]
        ]
        configured = self._platform_configured(platform, mock_mode)
        missing.extend(self._credential_missing(platform, mock_mode))

        redirect_uri = registry.redirect_uri(platform)
        https_required = platform in _HTTPS_REDIRECT_PLATFORMS
        redirect_ok = mock_mode or _redirect_ok(platform, redirect_uri)
        if not redirect_ok:
            if https_required:
                missing.append(
                    f"{platform.value}: the OAuth redirect URI is not https - set "
                    "RUNWAY_OAUTH_REDIRECT_BASE_URL to the public edge URL"
                )
            else:
                missing.append(
                    f"{platform.value}: Google accepts http only on a loopback host - "
                    "point RUNWAY_OAUTH_REDIRECT_BASE_URL at an https URL or "
                    "http://localhost / http://127.0.0.1"
                )
        connect_scopes = _connect_scopes(platform, settings)
        publishing_scopes = _publishing_scopes(platform, settings)
        app_status_field, app_status_env = _APP_STATUS_SETTINGS[platform]
        app_status_value = getattr(settings, app_status_field)

        accounts = self._account_views(
            platform,
            registry,
            connect_scopes=connect_scopes,
            publishing_scopes=publishing_scopes,
            mock_mode=mock_mode,
            now=now,
        )
        if not mock_mode:
            # Mock mode connects nothing: its accounts are fictional fixtures.
            missing.extend(self._account_missing(platform, accounts))
        notes.extend(self._platform_notes(platform, redirect_uri, app_status_value, mock_mode))
        if platform is Platform.tiktok and not mock_mode:
            if not settings.public_edge_url:
                missing.append(
                    "tiktok: set RUNWAY_PUBLIC_EDGE_URL to the public edge "
                    "(Tailscale Funnel) https URL - the OAuth callback and "
                    "webhooks must reach this machine"
                )
            elif urlsplit(settings.public_edge_url).scheme != "https":
                missing.append("tiktok: RUNWAY_PUBLIC_EDGE_URL must be an https URL")

        capabilities = [
            item.as_dict()
            for item in account_publishing_capabilities(
                platform,
                account=None,
                app=AppStatus.from_settings(settings),
                profile=settings.profile,
                implemented=self.implemented,
                media_host_available=settings.r2_configured,
                mock=mock_mode,
            )
        ]
        ready = not missing
        view = PlatformView(
            platform=platform.value,
            configured=configured,
            credentials=credentials,
            redirect_uri=redirect_uri,
            redirect_https_required=https_required,
            redirect_ok=redirect_ok,
            connect_scopes=connect_scopes,
            publishing_scopes=publishing_scopes,
            app_status=app_status_value,
            app_status_setting=app_status_env,
            accounts=accounts,
            capabilities=capabilities,
            notes=notes,
            missing=missing,
            ready=ready,
        )
        return view

    def _platform_configured(self, platform: Platform, mock_mode: bool) -> bool:
        if mock_mode:
            return True
        if platform is Platform.youtube:
            return self.settings.youtube_oauth_configured
        if platform is Platform.instagram:
            return (
                self.settings.instagram_oauth_configured
                or self.settings.instagram_unified_oauth_configured
            )
        return self.settings.tiktok_oauth_configured

    def _credential_missing(self, platform: Platform, mock_mode: bool) -> list[str]:
        """One line per missing credential; Unified Login is an either/or set."""
        if mock_mode:
            return []
        items = _PLATFORM_CREDENTIALS[platform]
        unset = [item for item in items if not getattr(self.settings, item.field)]
        if platform is Platform.instagram:
            base_ok = not any(item.adapter == "instagram" for item in unset)
            unified_ok = not any(item.adapter == "instagram_unified" for item in unset)
            if base_ok or unified_ok:
                return []
            return [
                "instagram: set RUNWAY_INSTAGRAM_APP_ID and "
                "RUNWAY_INSTAGRAM_APP_SECRET (Instagram Login) - or the Unified "
                "Login set RUNWAY_META_APP_ID / RUNWAY_META_APP_SECRET / "
                "RUNWAY_INSTAGRAM_UNIFIED_CONFIG_ID"
            ]
        return [f"{platform.value}: set {item.env_name} ({item.label})" for item in unset]

    def _account_views(
        self,
        platform: Platform,
        registry: ProviderRegistry,
        *,
        connect_scopes: list[str],
        publishing_scopes: list[str],
        mock_mode: bool,
        now: datetime,
    ) -> list[AccountView]:
        with self.database.session() as session:
            accounts = list(
                session.scalars(
                    select(ConnectedAccount)
                    .where(ConnectedAccount.platform == platform.value)
                    .order_by(ConnectedAccount.id)
                )
            )
        store = registry.token_store
        views: list[AccountView] = []
        for account in accounts:
            try:
                granted = json.loads(account.granted_scopes_json or "[]")
                granted = [str(scope) for scope in granted] if isinstance(granted, list) else []
            except json.JSONDecodeError:
                granted = []
            try:
                metadata = json.loads(account.metadata_json or "{}")
            except json.JSONDecodeError:
                metadata = {}
            connection = str(metadata.get("connection") or platform.value)
            scopes_source = metadata.get("scopes_source")
            expanded = equivalent_scopes(granted)
            unreadable = False
            token: TokenSet | None = None
            if account.token_ref:
                try:
                    token = store.load(account.token_ref)
                except TokenStoreError:
                    unreadable = True
            app = AppStatus.from_settings(self.settings)
            publishing = [
                item.as_dict()
                for item in account_publishing_capabilities(
                    platform,
                    account=AccountFacts(
                        status=account.status,
                        granted_scopes=tuple(granted),
                        connection=connection,
                    ),
                    app=app,
                    profile=self.settings.profile,
                    implemented=self.implemented,
                    media_host_available=self.settings.r2_configured,
                    mock=mock_mode,
                )
            ]
            views.append(
                AccountView(
                    id=account.id,
                    handle=account.handle,
                    status=account.status,
                    connection=connection,
                    granted_scopes=granted,
                    scopes_source=str(scopes_source) if scopes_source else None,
                    missing_connect_scopes=[
                        scope for scope in connect_scopes if scope not in expanded
                    ],
                    missing_publishing_scopes=[
                        scope for scope in publishing_scopes if scope not in expanded
                    ],
                    token=_token_view(token, now, unreadable=unreadable),
                    publishing=publishing,
                )
            )
        return views

    @staticmethod
    def _account_missing(platform: Platform, accounts: list[AccountView]) -> list[str]:
        """Reauth/reconnect lines; a healthy connected token satisfies this."""
        missing: list[str] = []
        usable = False
        for account in accounts:
            label = f"@{account.handle}" if account.handle else f"account {account.id}"
            if account.status == "needs_reauth":
                missing.append(
                    f"{platform.value}: reconnect {label} - the grant was "
                    "revoked or expired (status needs_reauth)"
                )
            elif account.status == "connected":
                if account.token.present and account.token.state in {"valid", "refreshable"}:
                    usable = True
                else:
                    state = (
                        "unreadable (the token key changed?)"
                        if account.token.state == "unreadable"
                        else "missing"
                        if not account.token.present
                        else account.token.state.replace("_", " ")
                    )
                    missing.append(
                        f"{platform.value}: reconnect {label} - its stored token is {state}"
                    )
        if not usable and not missing:
            missing.append(
                f"{platform.value}: connect your {platform.value} account in the "
                "web app (Accounts > Connect)"
            )
        return missing

    def _platform_notes(
        self,
        platform: Platform,
        redirect_uri: str,
        app_status_value: str,
        mock_mode: bool,
    ) -> list[str]:
        settings = self.settings
        notes: list[str] = []
        if mock_mode:
            notes.append(
                "mock provider mode - OAuth connect is disabled and accounts are fictional"
            )
            return notes
        if platform is Platform.youtube:
            if app_status_value == "unverified":
                notes.append(
                    "Google OAuth consent 'In production' without verification: the "
                    "consent screen shows an 'unverified app' warning and the app is "
                    "capped at 100 users - fine for the owner's own account"
                )
            if not settings.youtube_posts_api_access:
                notes.append(
                    "YouTube posts API pilot off (RUNWAY_YOUTUBE_POSTS_API_ACCESS): "
                    "youtube_post stays app-gated and the browser path publishes"
                )
            if not settings.public_edge_url:
                notes.append("no public edge - YouTube data arrives by polling instead of WebSub")
        elif platform is Platform.instagram:
            if not _redirect_ok(Platform.instagram, redirect_uri):
                notes.append(
                    "without an https redirect the owner can still connect by "
                    "importing a token (Accounts > import)"
                )
            elif not settings.public_edge_url:
                notes.append("no public edge - Instagram webhooks cannot reach this machine")
            if app_status_value == "development":
                notes.append(
                    "Meta app in development mode: connect works for accounts "
                    "with a role on the app"
                )
        elif platform is Platform.tiktok:
            if app_status_value == "sandbox":
                notes.append(
                    "sandbox app: direct posts (video.publish) stay off; inbox drafts "
                    "need a target user added in the TikTok developer portal"
                )
            elif app_status_value == "production":
                notes.append(
                    "production app not yet audited: TikTok has not granted "
                    "video.publish; finish the audit for direct posts"
                )
        return notes


__all__ = [
    "AccountView",
    "EdgeProbe",
    "EdgeView",
    "InstanceView",
    "PlatformView",
    "PlatformsDoctor",
    "PlatformsDoctorReport",
    "PublishingView",
    "SettingView",
    "TokenView",
]
