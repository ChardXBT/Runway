from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed Runway settings; secret values are never returned by the API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="RUNWAY_",
        extra="ignore",
    )

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
    capture_scroll_delay_ms: int = 1800
    capture_idle_cycles_before_stop: int = Field(default=30, ge=3, le=300)
    capture_idle_seconds_before_stop: int = Field(default=90, ge=15, le=900)
    capture_checkpoint_every: int = 10

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

    codex_cli_path: Path | None = None
    codex_model: str = "gpt-5.6-luna"
    codex_reasoning_effort: Literal["none", "low", "medium"] = "low"
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
    duplicate_perceptual_threshold: float = 0.94
    duplicate_semantic_threshold: float = 0.99
    caption_duplicate_threshold: float = 0.92
    caption_question_first: bool = True
    publisher_channel_id: str = "UCQ-nHijGwxNU3Go_wyLQ5Ng"
    publisher_confirmation_ttl_minutes: int = Field(default=10, ge=2, le=30)
    publisher_browser_channel: Literal["chrome", "chromium"] = "chrome"
    publisher_chrome_path: Path | None = None

    @field_validator("host")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        if value not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Runway may only bind to a loopback host")
        return value

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
    def publisher_profile_dir(self) -> Path:
        return self.browser_profile_dir / "publisher"

    @property
    def publisher_channel_url(self) -> str:
        return f"https://www.youtube.com/channel/{self.publisher_channel_id}/posts"

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
            "product_name": self.product_name,
            "connector_account_email": self.connector_account_email,
            "channel_name": self.channel_name,
            "channel_handle": self.channel_handle,
            "timezone": self.timezone,
            "default_post_time": self.default_post_time,
            "posts_per_day": self.posts_per_day,
            "scheduling_horizon_days": None,
            "approval_schedules_automatically": True,
            "duplicate_window_days": self.duplicate_window_days,
            "approval_required": self.approval_required,
            "publishing_enabled": self.publishing_enabled,
            "publisher_channel_id": self.publisher_channel_id,
            "publisher_confirmation_ttl_minutes": self.publisher_confirmation_ttl_minutes,
            "publisher_browser_channel": self.publisher_browser_channel,
            "publisher_approval_is_confirmation": True,
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
            "search_api_configured": bool(self.search_api_url and self.search_api_key),
            "representation_providers": {
                "text": self.text_embedding_provider,
                "multimodal": self.multimodal_embedding_provider,
                "device": self.embedding_device,
                "automatic_downloads": False,
                "implicit_activation": False,
            },
            "data_dir": str(self.resolved_data_dir),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
