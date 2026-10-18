from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, date, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, field_validator

from runway.platforms.capabilities import CapabilityEntry, Platform

T = TypeVar("T")


def iso_utc(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


class TokenSet(BaseModel):
    access_token: str
    refresh_token: str | None = None
    expires_at: datetime | None = None
    scopes: list[str] = []
    token_type: str = "Bearer"
    extra: dict[str, str] = {}


class AccountIdentity(BaseModel):
    external_account_id: str
    handle: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    profile_url: str | None = None
    account_type: str | None = None
    followers: int | None = None
    following: int | None = None
    content_count: int | None = None
    total_views: int | None = None
    total_likes: int | None = None
    raw: dict[str, Any] = {}

    @field_validator("handle", mode="before")
    @classmethod
    def _normalize_handle(cls, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        stripped = value.strip().lstrip("@")
        return stripped or None


class ContentItem(BaseModel):
    external_id: str
    content_type: str = "unknown"
    title: str | None = None
    caption: str | None = None
    permalink: str | None = None
    thumbnail_url: str | None = None
    published_at: datetime | None = None
    duration_seconds: int | None = None
    privacy_status: str | None = None
    metrics: dict[str, int | float | None] = {}
    raw: dict[str, Any] = {}


class Page(BaseModel, Generic[T]):
    items: list[T]
    next_cursor: dict[str, Any] | None = None
    quota_units: int = 0


class CommentItem(BaseModel):
    external_id: str
    content_external_id: str | None = None
    parent_external_id: str | None = None
    author_name: str | None = None
    author_external_id: str | None = None
    author_avatar_url: str | None = None
    author_is_owner: bool = False
    text: str
    like_count: int | None = None
    reply_count: int | None = None
    published_at: datetime | None = None
    platform_updated_at: datetime | None = None
    permalink: str | None = None
    raw: dict[str, Any] = {}


class DailyMetric(BaseModel):
    day: date
    metric: str
    value: float
    source: str


class ReplyResult(BaseModel):
    external_id: str
    verified: bool
    detail: str


class ProviderError(Exception):
    def __init__(
        self,
        detail: str,
        *,
        retryable: bool = False,
        status: int | None = None,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.retryable = retryable
        self.status = status


class AuthorizationExpired(ProviderError):
    """401 or invalid_grant; the account must be re-authorized."""


class PermissionMissing(ProviderError):
    """403 insufficient scope; the step is skipped and recorded."""


class RateLimited(ProviderError):
    """429 or quota exceeded; the run ends partial and can retry later."""

    def __init__(
        self,
        detail: str,
        *,
        retryable: bool = True,
        status: int | None = None,
    ) -> None:
        super().__init__(detail, retryable=retryable, status=status)


class CapabilityUnsupported(ProviderError):
    """The provider does not offer this operation."""


REDACTED_TOKEN_FIELDS = frozenset({"access_token", "refresh_token", "client_secret", "code"})


def redact_payload(payload: dict[str, Any]) -> dict[str, Any]:
    redacted: dict[str, Any] = {}
    for key, value in payload.items():
        if key in REDACTED_TOKEN_FIELDS and value is not None:
            redacted[key] = "[redacted]"
        elif isinstance(value, dict):
            redacted[key] = redact_payload(value)
        else:
            redacted[key] = value
    return redacted


def serialize_token(token: TokenSet) -> dict[str, Any]:
    return token.model_dump(mode="json")


def deserialize_token(payload: dict[str, Any]) -> TokenSet:
    return TokenSet.model_validate(payload)


class PlatformProvider(ABC):
    platform: Platform

    @abstractmethod
    def capabilities(self) -> list[CapabilityEntry]: ...

    @abstractmethod
    def configured(self) -> bool: ...

    @abstractmethod
    def authorization_url(
        self,
        *,
        state: str,
        redirect_uri: str,
        code_challenge: str | None,
    ) -> str: ...

    @abstractmethod
    def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None,
    ) -> TokenSet: ...

    @abstractmethod
    def refresh(self, token: TokenSet) -> TokenSet: ...

    @abstractmethod
    def revoke(self, token: TokenSet) -> bool: ...

    @abstractmethod
    def fetch_identity(self, token: TokenSet) -> AccountIdentity: ...

    @abstractmethod
    def list_content(
        self,
        token: TokenSet,
        *,
        cursor: dict[str, Any] | None,
        limit: int,
    ) -> Page[ContentItem]: ...

    @abstractmethod
    def fetch_content_metrics(
        self,
        token: TokenSet,
        external_ids: list[str],
    ) -> dict[str, dict[str, int | float | None]]: ...

    @abstractmethod
    def fetch_account_daily_metrics(
        self,
        token: TokenSet,
        *,
        since: date,
        until: date,
    ) -> list[DailyMetric]: ...

    @abstractmethod
    def list_comments(
        self,
        token: TokenSet,
        *,
        content_external_id: str,
        cursor: dict[str, Any] | None,
        limit: int,
    ) -> Page[CommentItem]: ...

    @abstractmethod
    def reply_to_comment(
        self,
        token: TokenSet,
        *,
        comment_external_id: str,
        text: str,
    ) -> ReplyResult: ...
