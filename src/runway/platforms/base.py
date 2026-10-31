from __future__ import annotations

import re
from abc import ABC, abstractmethod
from datetime import UTC, date, datetime
from typing import Any, Generic, TypeVar
from urllib.parse import urlsplit

import httpx
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
        delivery_unknown: bool = False,
    ) -> None:
        super().__init__(detail)
        self.detail = detail
        self.retryable = retryable
        self.status = status
        # True when the request may have reached the platform before the
        # failure (a read timeout, a dropped connection). For a write such as
        # a comment reply the outcome is then unknown, not failed.
        self.delivery_unknown = delivery_unknown


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


_URL_PATTERN = re.compile(r"[a-zA-Z][a-zA-Z0-9+.-]*://[^\s'\")>\]]+")
_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b(access_token|refresh_token|id_token|client_secret|client_assertion|"
    r"code_verifier|api[-_]?key|authorization|token|secret|password|signature|sig)"
    r"(\s*[=:]\s*)[^\s&'\")]+"
)
_PERSISTED_ERROR_LIMIT = 500


def safe_error_summary(exc: BaseException) -> str:
    """Return a one-line error string that is safe to persist.

    httpx exception messages can embed the full request URL, and some providers
    still place credentials in query parameters, so anything stored in the
    database (sync run summaries, account ``last_error``, action errors) keeps
    only ``scheme://host/path`` for each URL and has credential-shaped
    ``key=value`` fragments redacted.
    """
    if isinstance(exc, ProviderError):
        text: str = exc.detail
    else:
        text = str(exc).strip() or type(exc).__name__

    def _scrub_url(match: re.Match[str]) -> str:
        try:
            parsed = urlsplit(match.group(0))
        except ValueError:
            return "[url]"
        if not parsed.scheme or not parsed.netloc:
            return "[url]"
        return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

    text = _URL_PATTERN.sub(_scrub_url, text)
    text = _CREDENTIAL_PATTERN.sub(r"\1\2[redacted]", text)
    return text[:_PERSISTED_ERROR_LIMIT]


# Transport failures that happen before any request byte can reach the
# platform. Every other failure (read and write timeouts, a connection dropped
# mid-exchange) may follow a request the platform already acted on.
_NOT_SENT_ERRORS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.PoolTimeout,
    httpx.UnsupportedProtocol,
)


def request_may_have_been_delivered(exc: BaseException) -> bool:
    """False only when the failure provably happened before sending."""
    if isinstance(exc, ProviderError):
        return exc.delivery_unknown
    return not isinstance(exc, _NOT_SENT_ERRORS)


def provider_request(
    client: httpx.Client,
    provider: str,
    method: str,
    url: str,
    **kwargs: Any,
) -> httpx.Response:
    """Send an httpx request, translating transport failures into ProviderError.

    Status-code failures are still translated by each provider's own response
    handler; this only normalizes the exceptions raised before a response
    exists (connect, read, and pool timeouts, DNS failures, TLS and protocol
    errors) so callers record them as ordinary provider failures instead of
    crashing a sync run.
    """
    try:
        return client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        raise ProviderError(
            f"{provider} request failed: {safe_error_summary(exc)}",
            retryable=True,
            delivery_unknown=request_may_have_been_delivered(exc),
        ) from exc


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
