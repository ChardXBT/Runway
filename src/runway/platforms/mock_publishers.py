"""Mock publishers for `RUNWAY_PLATFORM_PROVIDER=mock` (spec 08 section 6).

They walk the full target machine on a timer without any network call:
send -> processing -> published -> verified (TikTok inbox: -> needs_user ->
published). A scenario forces each failure the engine must handle:

| Scenario | What happens |
| --- | --- |
| `success` | the normal path |
| `uncertain` | a read timeout right after the point of no return; the post *was* created, so reconciliation finds it |
| `container_error` | the platform accepts, then processing reports `ERROR` (final) |
| `quota_exceeded` | refused before sending with a quota error (retry after the reset) |
| `revoked_token` | the authorization is gone (final; reconnect) |
| `processing_failure` | processing fails after the upload (final) |

The scenario is `RUNWAY_MOCK_PUBLISH_SCENARIO`, or per target a caption
marker `[mock:<scenario>]`. Outcomes are derived from the scenario and the
payload hash, so a restarted worker reconciles to the same truth.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, get_args

import httpx

from runway.config import Settings
from runway.db.models import utcnow
from runway.platforms.capabilities import Platform
from runway.platforms.publisher import (
    Issue,
    PollResult,
    Progress,
    PublishAuthRevoked,
    PublishContext,
    PublishRetryLater,
    RateGuard,
    ReconcileResult,
    SendResult,
    VerifyResult,
    pacific_day_limit,
    pending_user_limit,
    rolling_limit,
)
from runway.posts.types import TargetPostType

Scenario = Literal[
    "success",
    "uncertain",
    "container_error",
    "quota_exceeded",
    "revoked_token",
    "processing_failure",
]
SCENARIOS: tuple[str, ...] = get_args(Scenario)
_MARKER = re.compile(r"\[mock:([a-z_]+)\]")
_CREATED = frozenset({"success", "uncertain", "container_error", "processing_failure"})


def scenario_for(ctx: PublishContext, default: str) -> str:
    match = _MARKER.search(ctx.caption or "") or _MARKER.search(ctx.title or "")
    if match and match.group(1) in SCENARIOS:
        return match.group(1)
    return default if default in SCENARIOS else "success"


def mock_external_id(ctx: PublishContext) -> str:
    return f"mock-{ctx.platform.value}-{ctx.target_id}-{ctx.payload_hash[:10]}"


class MockPublisher:
    kind = "mock"

    def __init__(
        self,
        name: str,
        platform: Platform,
        post_types: frozenset[TargetPostType],
        *,
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
        rate_guards: tuple[RateGuard, ...] = (),
        prepare_lead: timedelta = timedelta(0),
        draft_only: bool = False,
    ) -> None:
        self.name = name
        self.platform = platform
        self.post_types = post_types
        self.settings = settings
        self.clock = clock
        self.rate_guards = rate_guards
        self.prepare_lead = prepare_lead
        self.draft_only = draft_only
        self.processing = timedelta(seconds=settings.mock_publish_processing_seconds)
        # Every call, for tests and the demo log; never holds secrets.
        self.calls: list[tuple[str, int]] = []

    def supports(self, post_type: str) -> bool:
        return post_type in {member.value for member in self.post_types}

    def _scenario(self, ctx: PublishContext) -> str:
        return scenario_for(ctx, self.settings.mock_publish_scenario)

    def preflight(self, ctx: PublishContext) -> list[Issue]:
        self.calls.append(("preflight", ctx.target_id))
        return []

    def prepare(self, ctx: PublishContext) -> Mapping[str, Any]:
        """Instagram: create the container before `planned_at` (cannot publish)."""
        self.calls.append(("prepare", ctx.target_id))
        return {"container_id": f"mock-container-{ctx.target_id}-{ctx.payload_hash[:8]}"}

    def send(self, ctx: PublishContext, progress: Progress) -> SendResult:
        self.calls.append(("send", ctx.target_id))
        scenario = self._scenario(ctx)
        if scenario == "revoked_token":
            raise PublishAuthRevoked("mock: the account's authorization was revoked")
        if scenario == "quota_exceeded":
            raise PublishRetryLater(
                "quota_exceeded",
                "mock: the daily quota is used up",
                retry_after=ctx.now() + timedelta(hours=12),
            )
        total = sum(media.size_bytes for media in ctx.media) or 1
        progress.report(0, total, stage="uploading")
        progress.report(total, total, stage="uploaded")
        ctx.mark_request_sent()
        if scenario == "uncertain":
            raise httpx.ReadTimeout("mock: no response after the final request")
        return SendResult(
            status="processing",
            external_id=mock_external_id(ctx),
            detail="mock: the platform is processing the post",
            next_poll_seconds=max(int(self.processing.total_seconds()), 1),
            response={"mock": True, "accepted_at": ctx.now().isoformat()},
        )

    def _elapsed(self, ctx: PublishContext) -> timedelta:
        sent = ctx.request_sent_at
        if sent is None:
            return timedelta(0)
        if sent.tzinfo is None:
            sent = sent.replace(tzinfo=UTC)
        return ctx.now() - sent

    def poll(self, ctx: PublishContext) -> PollResult:
        self.calls.append(("poll", ctx.target_id))
        scenario = self._scenario(ctx)
        elapsed = self._elapsed(ctx)
        wait = max(int(self.processing.total_seconds()), 1)
        if elapsed < self.processing:
            return PollResult(status="processing", next_poll_seconds=wait)
        if scenario == "container_error":
            return PollResult(
                status="failed",
                error_code="container_error",
                detail="mock: the container reported ERROR (aspect ratio or codec)",
            )
        if scenario == "processing_failure":
            return PollResult(
                status="failed",
                error_code="processing_failed",
                detail="mock: the platform could not process the file",
            )
        if self.draft_only and elapsed < self.processing * 2:
            return PollResult(
                status="needs_user",
                detail="mock: the draft is in the creator's inbox",
                next_poll_seconds=wait,
            )
        return PollResult(
            status="published",
            external_id=mock_external_id(ctx),
            permalink=f"https://mock.invalid/{self.platform.value}/{mock_external_id(ctx)}",
        )

    def verify(self, ctx: PublishContext) -> VerifyResult:
        self.calls.append(("verify", ctx.target_id))
        if ctx.external_id != mock_external_id(ctx):
            return VerifyResult(verified=False, mismatches=("external id",))
        return VerifyResult(
            verified=True,
            permalink=f"https://mock.invalid/{self.platform.value}/{ctx.external_id}",
            detail="mock: read back and matched",
        )

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        self.calls.append(("reconcile", ctx.target_id))
        checked = (f"mock {self.platform.value}: looked up the post by its expected id",)
        if ctx.request_sent_at is None or self._scenario(ctx) not in _CREATED:
            return ReconcileResult(status="absent", checked=checked, detail="mock: not found")
        return ReconcileResult(
            status="published",
            external_id=mock_external_id(ctx),
            permalink=f"https://mock.invalid/{self.platform.value}/{mock_external_id(ctx)}",
            checked=checked,
        )


def mock_publishers(
    settings: Settings, *, clock: Callable[[], datetime] = utcnow
) -> list[MockPublisher]:
    youtube = frozenset({TargetPostType.YOUTUBE_VIDEO, TargetPostType.YOUTUBE_SHORT})
    instagram = frozenset(
        {
            TargetPostType.INSTAGRAM_REEL,
            TargetPostType.INSTAGRAM_IMAGE,
            TargetPostType.INSTAGRAM_CAROUSEL,
            TargetPostType.INSTAGRAM_STORY,
        }
    )
    day = timedelta(hours=24)
    return [
        MockPublisher(
            "youtube_direct",
            Platform.youtube,
            youtube,
            settings=settings,
            clock=clock,
            rate_guards=(pacific_day_limit(100, scope="platform"),),
        ),
        MockPublisher(
            "youtube_post",
            Platform.youtube,
            frozenset({TargetPostType.YOUTUBE_POST}),
            settings=settings,
            clock=clock,
        ),
        MockPublisher(
            "instagram_direct",
            Platform.instagram,
            instagram,
            settings=settings,
            clock=clock,
            rate_guards=(rolling_limit(50, day, label="posts"),),
            prepare_lead=timedelta(minutes=30),
        ),
        MockPublisher(
            "tiktok_direct",
            Platform.tiktok,
            frozenset({TargetPostType.TIKTOK_VIDEO}),
            settings=settings,
            clock=clock,
            rate_guards=(rolling_limit(15, day, label="posts"),),
        ),
        MockPublisher(
            "tiktok_inbox",
            Platform.tiktok,
            frozenset({TargetPostType.TIKTOK_DRAFT}),
            settings=settings,
            clock=clock,
            rate_guards=(pending_user_limit(5),),
            draft_only=True,
        ),
    ]
