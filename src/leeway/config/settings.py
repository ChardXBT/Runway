from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Typed Leeway settings; secret values are never returned by the API."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="LEWAY_",
        extra="ignore",
    )

    product_name: str = "Leeway"
    channel_name: str = "Qlob"
    channel_handle: str = "Qlob"
    timezone: str = "America/Toronto"
    default_post_time: str = "10:00"
    planning_horizon_days: int = 10
    posts_per_day: int = 1
    duplicate_window_days: int = 180
    approval_required: bool = True
    publishing_enabled: bool = False
    capture_scroll_delay_ms: int = 1800
    capture_idle_cycles_before_stop: int = 5
    capture_checkpoint_every: int = 10

    data_dir: Path = Path("data")
    host: str = "127.0.0.1"
    api_port: int = 8000
    web_port: int = 3000
    agent_runtime: Literal["mock", "codex", "openai"] = "mock"
    enable_browser_search: bool = False
    browser_search_url: str = "https://www.google.com/search?tbm=isch&q={query}"

    codex_cli_path: Path | None = None
    codex_model: str = "gpt-5.6-luna"
    codex_reasoning_effort: Literal["none", "low", "medium"] = "low"
    codex_timeout_seconds: int = Field(default=300, ge=30, le=1800)
    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_model: str | None = Field(default=None, validation_alias="OPENAI_MODEL")
    search_api_url: str | None = None
    search_api_key: str | None = None

    minimum_image_dimension: int = 480
    maximum_image_bytes: int = 20 * 1024 * 1024
    duplicate_perceptual_threshold: float = 0.94
    duplicate_semantic_threshold: float = 0.99
    caption_duplicate_threshold: float = 0.92

    @field_validator("host")
    @classmethod
    def loopback_only(cls, value: str) -> str:
        if value not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("Leeway may only bind to a loopback host")
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
        return self.resolved_data_dir / "leeway.db"

    @property
    def database_url(self) -> str:
        return f"sqlite:///{self.database_path.as_posix()}"

    @property
    def browser_profile_dir(self) -> Path:
        return self.resolved_data_dir / "browser-profile"

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
            self.resolved_data_dir / "reports",
            self.resolved_data_dir / "snapshots",
        ]
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)
        return directories

    def public_dict(self) -> dict[str, object]:
        return {
            "product_name": self.product_name,
            "channel_name": self.channel_name,
            "channel_handle": self.channel_handle,
            "timezone": self.timezone,
            "default_post_time": self.default_post_time,
            "planning_horizon_days": self.planning_horizon_days,
            "posts_per_day": self.posts_per_day,
            "duplicate_window_days": self.duplicate_window_days,
            "approval_required": self.approval_required,
            "publishing_enabled": False,
            "capture_scroll_delay_ms": self.capture_scroll_delay_ms,
            "capture_idle_cycles_before_stop": self.capture_idle_cycles_before_stop,
            "capture_checkpoint_every": self.capture_checkpoint_every,
            "agent_runtime": self.agent_runtime,
            "codex_model": self.codex_model,
            "codex_reasoning_effort": self.codex_reasoning_effort,
            "codex_chatgpt_auth_required": True,
            "paid_api_fallback_enabled": False,
            "openai_configured": bool(self.openai_api_key and self.openai_model),
            "browser_search_enabled": self.enable_browser_search,
            "search_api_configured": bool(self.search_api_url and self.search_api_key),
            "data_dir": str(self.resolved_data_dir),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
