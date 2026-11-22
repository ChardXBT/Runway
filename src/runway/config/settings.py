from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
# Identity providers with an implementation in runway.identity (spec 05 section 2).
SUPPORTED_IDENTITY_PROVIDERS = frozenset({"clerk"})


class Settings(BaseSettings):
    """Typed Runway settings; secret values are never returned by the API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="RUNWAY_",
        extra="ignore",
    )

    # `local` (default): one owner on loopback, no login. `hosted`: many
    # workspaces behind an identity provider (spec 01 section 2).
    profile: Literal["local", "hosted"] = "local"
    # Name of the configured identity provider; empty until one is set up.
    # Hosted mode refuses to start without it.
    identity_provider: str = ""
    # ---- Hosted profile (spec 05). `local` ignores every setting in this block
    # except `publishing_paused`, which only ever stops publishing.
    # Browser origins of the web app, comma-separated (https://app.example.com).
    # Defaults to the origin of RUNWAY_WEB_BASE_URL.
    web_origins: str = ""
    # Clerk: the instance's Frontend API URL is the token issuer. The JWKS URL
    # defaults to <issuer>/.well-known/jwks.json. Authorized parties (the `azp`
    # claim) default to the web origins.
    clerk_issuer: str = ""
    clerk_jwks_url: str = ""
    clerk_authorized_parties: str = ""
    clerk_audience: str = ""
    # Svix signing secret(s) of the Clerk webhook endpoint (`whsec_...`); several
    # may be given, space- or comma-separated, while one is being rotated.
    clerk_webhook_secret: SecretStr | None = None
    identity_token_leeway_seconds: int = Field(default=5, ge=0, le=60)
    # Runway role for a member who joins an existing workspace. The first member
    # of a new organization's workspace becomes its owner.
    hosted_default_member_role: Literal["viewer", "editor", "approver"] = "viewer"
    # Proxies whose X-Forwarded-For uvicorn trusts in `runway serve` (hosted only).
    forwarded_allow_ips: str = "127.0.0.1"
    rate_limit_ip_per_minute: int = Field(default=300, ge=1)
    rate_limit_principal_per_minute: int = Field(default=120, ge=1)
    # Default per-workspace quotas (spec 05 section 6); operators override them
    # per workspace with `runway hosted quota`. Hosted only: `local` is unlimited.
    quota_storage_bytes: int = Field(default=1024**3, ge=0)
    quota_connected_accounts: int = Field(default=3, ge=0)
    quota_posts_per_day: int = Field(default=10, ge=0)
    quota_api_requests_per_minute: int = Field(default=120, ge=1)
    # Operator switch read by the worker before every send, in both profiles.
    publishing_paused: bool = False
    product_name: str = "Runway"
    connector_account_email: str = "tryrunwaytoday@gmail.com"
    channel_name: str = "Qlob"
    channel_handle: str = "Qlob"
    timezone: str = "America/Toronto"
    default_post_time: str = "10:00"
    posts_per_day: int = Field(default=1, ge=1, le=1)
    duplicate_window_days: int = 180
    approval_required: bool = True
    publishing_enabled: bool = False
    publishing_mode: Literal["assisted", "authorized_browser"] = "assisted"
    youtube_automation_authorized: bool = False
    capture_scroll_delay_ms: int = 1800
    capture_idle_cycles_before_stop: int = Field(default=30, ge=3, le=300)
    capture_idle_seconds_before_stop: int = Field(default=90, ge=15, le=900)
    capture_checkpoint_every: int = Field(default=10, ge=1)

    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    api_port: int = 8000
    web_port: int = 3000
    agent_runtime: Literal["mock", "codex", "openai"] = "mock"
    enable_browser_search: bool = False
    browser_search_url: str = "https://www.bing.com/images/search?q={query}&safeSearch=Strict"
    browser_search_max_queries: int = Field(default=6, ge=1, le=10)
    browser_search_results_per_query: int = Field(default=3, ge=1, le=20)
    browser_search_max_results: int = Field(default=18, ge=1, le=100)
    discovery_primary_topic_query_share: float = Field(default=0.8, ge=0.5, le=1.0)
    discovery_secondary_topics: str = ""

    codex_cli_path: Path | None = None
    codex_model: str = "gpt-6-luna"
    codex_reasoning_effort: Literal["none", "low", "medium", "high"] = "high"
    codex_timeout_seconds: int = Field(default=300, ge=30, le=1800)
    analysis_batch_size: int = Field(default=5, ge=1, le=10)
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_model: str | None = Field(default=None, validation_alias="OPENAI_MODEL")
    search_api_url: str | None = None
    search_api_key: str | None = None

    text_embedding_provider: Literal["runway-local", "sentence-transformers"] = "runway-local"
    text_embedding_model_path: Path | None = None
    text_embedding_model_id: str = "Qwen/Qwen3-Embedding-0.6B"
    text_embedding_model_revision: str = "unvalidated-local"
    multimodal_embedding_provider: Literal["runway-local", "siglip2"] = "runway-local"
    multimodal_embedding_model_path: Path | None = None
    multimodal_embedding_model_id: str = "google/siglip2-base-patch16-224"
    multimodal_embedding_model_revision: str = "unvalidated-local"
    embedding_device: Literal["cpu", "cuda"] = "cpu"
    embedding_batch_size: int = Field(default=8, ge=1, le=128)

    minimum_image_dimension: int = 480
    maximum_image_bytes: int = 20 * 1024 * 1024
    # Decoded-size budget; far below Pillow's decompression-bomb threshold.
    maximum_image_pixels: int = Field(default=40_000_000, ge=1_000_000, le=89_000_000)
    duplicate_perceptual_threshold: float = 0.94
    duplicate_semantic_threshold: float = 0.99
    caption_duplicate_threshold: float = 0.92
    caption_question_first: bool = True
    publisher_channel_id: str = "UCxxxxxxxxxxxxxxxxxxxxxx"
    publisher_confirmation_ttl_minutes: int = Field(default=10, ge=2, le=30)
    publisher_browser_channel: Literal["chrome", "chromium"] = "chrome"
    publisher_chrome_path: Path | None = None

    platform_provider: Literal["live", "mock"] = "live"
    oauth_redirect_base_url: str | None = None
    web_base_url: str | None = None
    youtube_client_id: str | None = None
    youtube_client_secret: str | None = None
    youtube_engage_scopes: bool = False
    instagram_app_id: str | None = None
    instagram_app_secret: str | None = None
    tiktok_client_key: str | None = None
    tiktok_client_secret: str | None = None
    inbox_replies_enabled: bool = False
    platform_sync_interval_minutes: int = Field(default=0, ge=0, le=1440)
    platform_sync_max_content_items: int = Field(default=200, ge=10, le=2000)
    platform_sync_comment_content_limit: int = Field(default=10, ge=0, le=100)
    platform_sync_analytics_days: int = Field(default=90, ge=7, le=365)

    @field_validator(
        "codex_cli_path",
        "publisher_chrome_path",
        "text_embedding_model_path",
        "multimodal_embedding_model_path",
        mode="before",
    )
    @classmethod
    def blank_optional_path(cls, value: object) -> object | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator(
        "oauth_redirect_base_url",
        "web_base_url",
        "youtube_client_id",
        "youtube_client_secret",
        "instagram_app_id",
        "instagram_app_secret",
        "tiktok_client_key",
        "tiktok_client_secret",
        mode="before",
    )
    @classmethod
    def blank_optional_str(cls, value: object) -> object | None:
        if value is None or (isinstance(value, str) and not value.strip()):
            return None
        return value

    @field_validator("default_post_time")
    @classmethod
    def valid_time(cls, value: str) -> str:
        parts = value.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ValueError("default_post_time must be HH:MM")
        hour, minute = (int(part) for part in parts)
        if hour not in range(24) or minute not in range(60):
            raise ValueError("default_post_time must be a valid local time")
        return f"{hour:02d}:{minute:02d}"

    @model_validator(mode="after")
    def profile_requirements(self) -> Settings:
        """Fail fast on combinations a profile cannot run safely (spec 01 section 2)."""
        if self.profile == "local":
            if self.host not in LOOPBACK_HOSTS:
                raise ValueError("Runway may only bind to a loopback host")
            return self
        problems: list[str] = []
        if self.database_url.startswith("sqlite"):
            problems.append("a Postgres database (SQLite is local-only)")
        provider = self.identity_provider.strip()
        if not provider:
            problems.append("a configured identity provider (RUNWAY_IDENTITY_PROVIDER)")
        elif provider not in SUPPORTED_IDENTITY_PROVIDERS:
            problems.append(
                "RUNWAY_IDENTITY_PROVIDER set to one of "
                + ", ".join(sorted(SUPPORTED_IDENTITY_PROVIDERS))
            )
        elif provider == "clerk":
            problems.extend(self._clerk_problems())
        problems.extend(self._web_origin_problems())
        if self.publishing_mode == "authorized_browser":
            problems.append(
                "RUNWAY_PUBLISHING_MODE other than authorized_browser "
                "(the browser publisher is local-only)"
            )
        if problems:
            raise ValueError("RUNWAY_PROFILE=hosted requires " + "; ".join(problems))
        return self

    def _clerk_problems(self) -> list[str]:
        problems: list[str] = []
        if not _is_https_url(self.clerk_issuer):
            problems.append("an https Clerk issuer (RUNWAY_CLERK_ISSUER)")
        if self.clerk_jwks_url.strip() and not _is_https_url(self.clerk_jwks_url):
            problems.append("an https Clerk JWKS URL (RUNWAY_CLERK_JWKS_URL)")
        if not self.clerk_webhook_secrets:
            problems.append("a Clerk webhook signing secret (RUNWAY_CLERK_WEBHOOK_SECRET)")
        return problems

    def _web_origin_problems(self) -> list[str]:
        origins = self.hosted_web_origin_list
        if not origins:
            return ["at least one web origin (RUNWAY_WEB_ORIGINS)"]
        invalid = [origin for origin in origins if not _is_web_origin(origin)]
        if invalid:
            return [
                "web origins of the form https://host[:port] (RUNWAY_WEB_ORIGINS): "
                + ", ".join(invalid)
            ]
        return []

    @model_validator(mode="after")
    def authorized_browser_requires_both_interlocks(self) -> Settings:
        if self.publishing_mode != "authorized_browser":
            return self
        missing: list[str] = []
        if not self.publishing_enabled:
            missing.append("RUNWAY_PUBLISHING_ENABLED=true")
        if not self.youtube_automation_authorized:
            missing.append("RUNWAY_YOUTUBE_AUTOMATION_AUTHORIZED=true")
        if missing:
            raise ValueError(
                "RUNWAY_PUBLISHING_MODE=authorized_browser requires " + " and ".join(missing)
            )
        return self

    @property
    def authorized_browser_ready(self) -> bool:
        return (
            self.publishing_mode == "authorized_browser"
            and self.publishing_enabled
            and self.youtube_automation_authorized
        )

    @property
    def project_root(self) -> Path:
        return Path(__file__).resolve().parents[3]

    @property
    def resolved_data_dir(self) -> Path:
        if self.data_dir.is_absolute():
            return self.data_dir
        return (self.project_root / self.data_dir).resolve()

    @property
    def database_path(self) -> Path:
        return self.resolved_data_dir / "runway.db"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path.as_posix()}"

    @property
    def browser_profile_dir(self) -> Path:
        return self.resolved_data_dir / "browser-profile"

    @property
    def discovery_secondary_topic_list(self) -> list[str]:
        """Explicit creator-approved rotation topics, in configured order."""
        return list(
            dict.fromkeys(
                value.strip()
                for value in self.discovery_secondary_topics.split(",")
                if value.strip()
            )
        )

    @property
    def publisher_profile_dir(self) -> Path:
        return self.browser_profile_dir / "publisher"

    @property
    def publisher_channel_url(self) -> str:
        return f"https://www.youtube.com/channel/{self.publisher_channel_id}/posts"

    @property
    def resolved_oauth_redirect_base_url(self) -> str:
        if self.oauth_redirect_base_url:
            return self.oauth_redirect_base_url.rstrip("/")
        return f"http://127.0.0.1:{self.api_port}"

    @property
    def resolved_web_base_url(self) -> str:
        if self.web_base_url:
            return self.web_base_url.rstrip("/")
        return f"http://127.0.0.1:{self.web_port}"

    @property
    def hosted_web_origin_list(self) -> list[str]:
        """Browser origins allowed in `hosted`, normalized, in configured order."""
        configured = _csv(self.web_origins)
        if not configured and self.web_base_url:
            parsed = urlsplit(self.web_base_url)
            if parsed.scheme and parsed.netloc:
                configured = [f"{parsed.scheme}://{parsed.netloc}"]
        return list(dict.fromkeys(origin.rstrip("/").casefold() for origin in configured))

    @property
    def clerk_jwks_endpoint(self) -> str:
        if self.clerk_jwks_url.strip():
            return self.clerk_jwks_url.strip()
        return self.clerk_issuer.strip().rstrip("/") + "/.well-known/jwks.json"

    @property
    def clerk_authorized_party_list(self) -> list[str]:
        return _csv(self.clerk_authorized_parties) or self.hosted_web_origin_list

    @property
    def clerk_webhook_secrets(self) -> list[str]:
        if self.clerk_webhook_secret is None:
            return []
        raw = self.clerk_webhook_secret.get_secret_value()
        return [part for part in raw.replace(",", " ").split() if part]

    @property
    def youtube_oauth_configured(self) -> bool:
        return bool(self.youtube_client_id and self.youtube_client_secret)

    @property
    def instagram_oauth_configured(self) -> bool:
        return bool(self.instagram_app_id and self.instagram_app_secret)

    @property
    def tiktok_oauth_configured(self) -> bool:
        return bool(self.tiktok_client_key and self.tiktok_client_secret)

    def ensure_directories(self) -> list[Path]:
        directories = [
            self.resolved_data_dir,
            self.resolved_data_dir / "raw",
            self.resolved_data_dir / "captures",
            self.resolved_data_dir / "media" / "historical",
            self.resolved_data_dir / "media" / "candidates",
            self.resolved_data_dir / "media" / "approved",
            self.resolved_data_dir / "media" / "previews",
            self.browser_profile_dir,
            self.publisher_profile_dir,
            self.resolved_data_dir / "captures" / "publisher",
            self.resolved_data_dir / "reports",
            self.resolved_data_dir / "snapshots",
        ]
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
        return directories

    def public_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "product_name": self.product_name,
            "connector_account_email": self.connector_account_email,
            "channel_name": self.channel_name,
            "channel_handle": self.channel_handle,
            "timezone": self.timezone,
            "default_post_time": self.default_post_time,
            "posts_per_day": self.posts_per_day,
            "scheduling_horizon_days": None,
            "approval_schedules_automatically": False,
            "duplicate_window_days": self.duplicate_window_days,
            "approval_required": self.approval_required,
            "publishing_enabled": self.publishing_enabled,
            "publishing_mode": self.publishing_mode,
            "publishing_paused": self.publishing_paused,
            "youtube_automation_authorized": self.youtube_automation_authorized,
            "authorized_browser_ready": self.authorized_browser_ready,
            "publisher_channel_id": self.publisher_channel_id,
            "publisher_confirmation_ttl_minutes": self.publisher_confirmation_ttl_minutes,
            "publisher_browser_channel": self.publisher_browser_channel,
            "publisher_approval_is_confirmation": False,
            "publisher_lineup_action_only": True,
            "publisher_visible_browser_only": True,
            "capture_scroll_delay_ms": self.capture_scroll_delay_ms,
            "capture_idle_cycles_before_stop": self.capture_idle_cycles_before_stop,
            "capture_idle_seconds_before_stop": self.capture_idle_seconds_before_stop,
            "capture_checkpoint_every": self.capture_checkpoint_every,
            "agent_runtime": self.agent_runtime,
            "codex_model": self.codex_model,
            "codex_reasoning_effort": self.codex_reasoning_effort,
            "analysis_batch_size": self.analysis_batch_size,
            "caption_question_first": self.caption_question_first,
            "codex_chatgpt_auth_required": True,
            "paid_api_fallback_enabled": False,
            "openai_configured": bool(self.openai_api_key and self.openai_model),
            "browser_search_enabled": self.enable_browser_search,
            "browser_search_limits": {
                "max_queries": self.browser_search_max_queries,
                "results_per_query": self.browser_search_results_per_query,
                "max_results": self.browser_search_max_results,
            },
            "discovery_topic_mix": {
                "primary_query_share": self.discovery_primary_topic_query_share,
                "secondary_topics": self.discovery_secondary_topic_list,
            },
            "search_api_configured": bool(self.search_api_url and self.search_api_key),
            "representation_providers": {
                "text": self.text_embedding_provider,
                "multimodal": self.multimodal_embedding_provider,
                "device": self.embedding_device,
                "automatic_downloads": False,
                "implicit_activation": False,
            },
            "platform_provider": self.platform_provider,
            "youtube_oauth_configured": self.youtube_oauth_configured,
            "instagram_oauth_configured": self.instagram_oauth_configured,
            "tiktok_oauth_configured": self.tiktok_oauth_configured,
            "inbox_replies_enabled": self.inbox_replies_enabled,
            "platform_sync_interval_minutes": self.platform_sync_interval_minutes,
            "oauth_redirect_base_url": self.resolved_oauth_redirect_base_url,
            "data_dir": str(self.resolved_data_dir),
        }


def _csv(value: str) -> list[str]:
    return [part.strip() for part in value.split(",") if part.strip()]


def _is_https_url(value: str) -> bool:
    parsed = urlsplit(value.strip())
    return parsed.scheme == "https" and bool(parsed.hostname)


def _is_web_origin(origin: str) -> bool:
    """https://host[:port] with no path; plain http only for a loopback rehearsal."""
    parsed = urlsplit(origin)
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment or not parsed.hostname:
        return False
    if parsed.username or parsed.password:
        return False
    if parsed.scheme == "https":
        return True
    return parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
