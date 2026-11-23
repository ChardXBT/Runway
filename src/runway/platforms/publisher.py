"""The Publisher port (spec 03 section 5): what every publishing adapter implements.

An adapter turns one released target into a post on one platform account. The
engine (`runway.posts.jobs`) owns everything around it: the attempt ledger,
the state machine, retries, reconciliation and events. An adapter only talks
to its platform and reports what happened, through these types.

Exactly-once rules an adapter must follow (spec 03 section 4):

1. Call `ctx.mark_request_sent()` immediately before the one request that can
   create the post (YouTube: the final byte range; Instagram: `media_publish`;
   TikTok direct: `init`). The engine commits the marker before it returns.
   A crash after the marker is reconciled, never re-sent.
2. Persist anything needed to resume (an upload session URI and offset) with
   `ctx.save_resumable(...)`; a re-run after a crash before the marker gets it
   back in `ctx.resumable`.
3. Raise `PublishRefused` when the platform answered "no" (nothing was
   published), `PublishRetryLater` when it answered "not now", and let
   transport errors propagate: the engine classifies them with
   `request_may_have_been_delivered`, so a read timeout after the marker
   becomes `uncertain` and is never retried automatically.
4. Never put tokens, signed URLs or secrets in any result, error or response
   summary (invariant 5).

Optional adapter attributes, read with `getattr` (all may be omitted):

- `platform: Platform | None` - the platform served (None: any, e.g. assisted).
- `kind: AdapterKind` - "direct", "draft", "vendor", "assisted" or "mock".
- `rate_guards: Sequence[RateGuard]` - checked before queuing and before sending.
- `prepare_lead: timedelta` with `prepare(ctx) -> Mapping[str, Any]` - work done
  up to that long before `planned_at` that cannot publish (an Instagram
  container). The returned mapping is handed to `send` in `ctx.resumable`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, BinaryIO, Literal, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

from runway.platforms.base import ProviderError, TokenSet, request_may_have_been_delivered
from runway.platforms.capabilities import Platform

AdapterKind = Literal["direct", "draft", "vendor", "assisted", "mock"]
Severity = Literal["error", "warning"]


@dataclass(frozen=True, slots=True)
class Issue:
    """A validation or preflight finding. `error` blocks release; `warning` does not.

    Field-compatible with `runway.mediastore.validation.Issue` (plus `field`).
    """

    severity: Severity
    code: str
    message: str
    fix: str | None = None
    source: str | None = None
    item: int | None = None
    field: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {key: value for key, value in asdict(self).items() if value is not None}


def has_errors(issues: Sequence[Issue]) -> bool:
    return any(issue.severity == "error" for issue in issues)


@dataclass(frozen=True, slots=True)
class AccountRef:
    id: int
    platform: Platform
    external_account_id: str
    handle: str | None
    status: str
    granted_scopes: tuple[str, ...]
    token_ref: str | None
    account_type: str | None = None


@dataclass(frozen=True, slots=True)
class MediaRef:
    """One media item of the post, in order. Bytes come from `ctx.open_media`."""

    asset_id: int
    position: int
    kind: str | None
    sha256: str
    size_bytes: int
    content_type: str
    storage: str
    storage_key: str
    original_filename: str | None = None
    probe: Mapping[str, Any] = field(default_factory=dict)


def _not_wired(name: str) -> Callable[..., Any]:
    def missing(*_args: Any, **_kwargs: Any) -> Any:
        raise NotImplementedError(f"PublishContext.{name} is not available in this context")

    return missing


@dataclass
class PublishContext:
    """Everything an adapter may know about one target, plus the engine's hooks."""

    workspace_id: int
    post_id: int
    target_id: int
    platform: Platform
    post_type: str
    publisher: str
    account: AccountRef | None
    title: str
    caption: str
    settings: Mapping[str, Any]
    media: tuple[MediaRef, ...]
    planned_at: datetime | None
    payload_hash: str
    ai_generated: bool
    external_id: str | None = None
    permalink: str | None = None
    attempt_id: int | None = None
    job_id: int | None = None
    # Resume state from the latest attempt for this payload (or `prepare`).
    resumable: Mapping[str, Any] = field(default_factory=dict)
    request_sent_at: datetime | None = None
    timezone: str = "UTC"
    clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    # Engine hooks. Adapters call these; tests may replace them.
    _mark_request_sent: Callable[[], datetime] = field(
        default_factory=lambda: _not_wired("mark_request_sent")
    )
    _save_resumable: Callable[[Mapping[str, Any]], None] = field(
        default_factory=lambda: _not_wired("save_resumable")
    )
    _open_media: Callable[[MediaRef], BinaryIO] = field(
        default_factory=lambda: _not_wired("open_media")
    )
    _signed_media_url: Callable[[MediaRef, int], str | None] = field(
        default_factory=lambda: _not_wired("signed_media_url")
    )
    _access_token: Callable[[], TokenSet] = field(
        default_factory=lambda: _not_wired("access_token")
    )

    def now(self) -> datetime:
        return self.clock()

    def mark_request_sent(self) -> datetime:
        """Commit the point-of-no-return marker; call right before the creating request."""
        if self.request_sent_at is None:
            self.request_sent_at = self._mark_request_sent()
        return self.request_sent_at

    def save_resumable(self, state: Mapping[str, Any]) -> None:
        """Persist resume state (never tokens or signed URLs) on the attempt."""
        self._save_resumable(dict(state))
        self.resumable = dict(state)

    def open_media(self, media: MediaRef) -> BinaryIO:
        """A seekable stream of the media bytes (ranged reads for chunked uploads)."""
        return self._open_media(media)

    def signed_media_url(self, media: MediaRef, ttl_seconds: int = 7200) -> str | None:
        """A short-lived URL a platform can fetch, or None without a public media host."""
        return self._signed_media_url(media, ttl_seconds)

    def access_token(self) -> TokenSet:
        """A fresh token for the target's account (refreshed under the account lock)."""
        return self._access_token()


class Progress(Protocol):
    def report(self, done: int, total: int | None = None, *, stage: str | None = None) -> None:
        """Bytes (or steps) done so far. The engine emits at most one event per second."""


class NullProgress:
    def report(self, done: int, total: int | None = None, *, stage: str | None = None) -> None:
        return None


SendStatus = Literal["published", "processing", "needs_user"]


@dataclass(frozen=True, slots=True)
class SendResult:
    """The platform accepted the request.

    `processing`: it is transcoding or moderating (the engine polls).
    `published`: it returned the post's id. `needs_user`: a person must finish
    it (TikTok inbox draft, the assisted checklist).
    """

    status: SendStatus
    external_id: str | None = None
    permalink: str | None = None
    detail: str | None = None
    response: Mapping[str, Any] = field(default_factory=dict)
    next_poll_seconds: int | None = None
    checklist: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PollResult:
    status: Literal["processing", "published", "needs_user", "failed"]
    external_id: str | None = None
    permalink: str | None = None
    detail: str | None = None
    error_code: str | None = None
    # For `failed`: the platform said a new attempt can work.
    retryable: bool = False
    next_poll_seconds: int | None = None
    response: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """`verified=None` means "cannot tell yet" (the engine asks again later)."""

    verified: bool | None
    permalink: str | None = None
    detail: str | None = None
    mismatches: tuple[str, ...] = ()
    response: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """What the platform says about a target whose outcome is unknown.

    `absent` must mean provably not published (and safe to send again);
    `uncertain` keeps the target waiting for a person. `checked` lists, in
    plain words, what was looked at, for the "what we checked" view.
    """

    status: Literal["published", "processing", "needs_user", "absent", "uncertain"]
    external_id: str | None = None
    permalink: str | None = None
    detail: str | None = None
    checked: tuple[str, ...] = ()
    candidates: tuple[Mapping[str, Any], ...] = ()
    response: Mapping[str, Any] = field(default_factory=dict)


class PublishError(Exception):
    """An adapter-reported failure.

    `delivered=False` means the platform answered and nothing was published.
    `delivered=None` means unknown: after the point of no return the engine
    treats it as `uncertain`.
    """

    def __init__(
        self,
        code: str,
        detail: str,
        *,
        final: bool = False,
        delivered: bool | None = None,
        retry_after: datetime | None = None,
    ) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail
        self.final = final
        self.delivered = delivered
        self.retry_after = retry_after


class PublishRefused(PublishError):
    """The platform refused the post; the same payload cannot work."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(code, detail, final=True, delivered=False)


class PublishRetryLater(PublishError):
    """The platform said "not now" (quota, 5xx with a response); nothing was published."""

    def __init__(self, code: str, detail: str, *, retry_after: datetime | None = None) -> None:
        super().__init__(code, detail, final=False, delivered=False, retry_after=retry_after)


class PublishAuthRevoked(PublishError):
    """The account's authorization is gone; the owner must reconnect it."""

    def __init__(self, detail: str = "the account must be reconnected") -> None:
        super().__init__("auth_revoked", detail, final=True, delivered=False)


FailureState = Literal["failed_retryable", "failed_final", "uncertain"]


@dataclass(frozen=True, slots=True)
class FailureClass:
    state: FailureState
    code: str
    detail: str
    retry_after: datetime | None = None


def classify_send_failure(
    exc: BaseException, *, request_sent: bool, detail: str | None = None
) -> FailureClass:
    """Spec 03 section 4 step 4: what a failed send means for the target.

    Before the point of no return nothing can have been created, so a failure
    is retryable (or final when the platform refused). After it, only a
    failure that provably happened before the request left (a connect error)
    or an explicit platform answer is a plain failure; everything else - a
    read timeout, a dropped connection, an unexpected exception - is
    `uncertain`.
    """
    text = detail or (exc.detail if isinstance(exc, PublishError | ProviderError) else str(exc))
    text = text or type(exc).__name__
    if isinstance(exc, PublishError):
        if exc.delivered is False:
            state: FailureState = "failed_final" if exc.final else "failed_retryable"
            return FailureClass(state, exc.code, text, exc.retry_after)
        if not request_sent:
            return FailureClass(
                "failed_final" if exc.final else "failed_retryable", exc.code, text
            )
        return FailureClass("uncertain", exc.code, text)
    if not request_sent:
        return FailureClass("failed_retryable", "not_sent", text)
    if not request_may_have_been_delivered(exc):
        if isinstance(exc, ProviderError) and not exc.retryable:
            return FailureClass("failed_final", "platform_refused", text)
        return FailureClass("failed_retryable", "not_delivered", text)
    return FailureClass("uncertain", "outcome_unknown", text)


# ----------------------------------------------------------------- rate guards


@dataclass(frozen=True, slots=True)
class RateDecision:
    allowed: bool
    detail: str = ""
    used: int | None = None
    limit: int | None = None
    retry_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class RateGuardContext:
    """What a guard may look at. `count_sent(since, scope)` counts sends in the ledger.

    `scope="account"` counts this target's account, `"platform"` every account
    of the platform in the install (YouTube's per-project upload counter).
    """

    workspace_id: int
    account_id: int | None
    platform: Platform
    post_type: str
    at: datetime
    count_sent: Callable[[datetime, Literal["account", "platform"]], int]
    count_pending_user: Callable[[datetime], int] = field(default=lambda _since: 0)


RateGuard = Callable[[RateGuardContext], RateDecision]


def rolling_limit(
    limit: int,
    window: timedelta,
    *,
    scope: Literal["account", "platform"] = "account",
    label: str = "posts",
) -> RateGuard:
    """At most `limit` sends in any `window` (Instagram 50 per 24 h, TikTok ~15 per day)."""

    def guard(ctx: RateGuardContext) -> RateDecision:
        used = ctx.count_sent(ctx.at - window, scope)
        if used < limit:
            return RateDecision(True, used=used, limit=limit)
        hours = window.total_seconds() / 3600
        return RateDecision(
            False,
            f"{used} of {limit} {label} in the last {hours:g} hours",
            used=used,
            limit=limit,
            retry_at=ctx.at + window,
        )

    return guard


PACIFIC = ZoneInfo("America/Los_Angeles")


def pacific_day_limit(
    limit: int, *, scope: Literal["account", "platform"] = "platform", label: str = "uploads"
) -> RateGuard:
    """At most `limit` sends per Pacific calendar day (YouTube quota resets at midnight PT)."""

    def guard(ctx: RateGuardContext) -> RateDecision:
        local = ctx.at.astimezone(PACIFIC)
        start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        used = ctx.count_sent(start.astimezone(UTC), scope)
        if used < limit:
            return RateDecision(True, used=used, limit=limit)
        next_day = (start + timedelta(days=1)).astimezone(UTC)
        return RateDecision(
            False,
            f"{used} of {limit} {label} today (Pacific time)",
            used=used,
            limit=limit,
            retry_at=next_day,
        )

    return guard


def pending_user_limit(limit: int, window: timedelta = timedelta(hours=24)) -> RateGuard:
    """At most `limit` drafts waiting for the creator (TikTok inbox: 5 per 24 h)."""

    def guard(ctx: RateGuardContext) -> RateDecision:
        used = ctx.count_pending_user(ctx.at - window)
        if used < limit:
            return RateDecision(True, used=used, limit=limit)
        return RateDecision(
            False,
            f"{used} of {limit} drafts are waiting in the creator's inbox",
            used=used,
            limit=limit,
            retry_at=ctx.at + window,
        )

    return guard


# ---------------------------------------------------------------------- port


@runtime_checkable
class Publisher(Protocol):
    """Spec 03 section 5. `name`: 'youtube_direct', 'instagram_direct',
    'tiktok_direct', 'tiktok_inbox', 'vendor:<name>', 'assisted' (or a mock)."""

    name: str

    def supports(self, post_type: str) -> bool: ...

    def preflight(self, ctx: PublishContext) -> list[Issue]: ...

    def send(self, ctx: PublishContext, progress: Progress) -> SendResult: ...

    def poll(self, ctx: PublishContext) -> PollResult: ...

    def verify(self, ctx: PublishContext) -> VerifyResult: ...

    def reconcile(self, ctx: PublishContext) -> ReconcileResult: ...


@runtime_checkable
class Preparer(Protocol):
    """Optional: work before `planned_at` that cannot publish (spec 03 section 6)."""

    prepare_lead: timedelta

    def prepare(self, ctx: PublishContext) -> Mapping[str, Any]: ...


def adapter_kind(publisher: Publisher) -> AdapterKind:
    kind = getattr(publisher, "kind", "direct")
    return kind if kind in {"direct", "draft", "vendor", "assisted", "mock"} else "direct"


def adapter_platform(publisher: Publisher) -> Platform | None:
    platform = getattr(publisher, "platform", None)
    return platform if isinstance(platform, Platform) else None


def adapter_rate_guards(publisher: Publisher) -> tuple[RateGuard, ...]:
    guards = getattr(publisher, "rate_guards", ())
    return tuple(guards) if guards else ()


def adapter_prepare_lead(publisher: Publisher) -> timedelta | None:
    if not isinstance(publisher, Preparer):
        return None
    lead = publisher.prepare_lead
    return lead if lead > timedelta(0) else None
