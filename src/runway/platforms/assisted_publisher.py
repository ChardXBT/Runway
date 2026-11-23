"""The `assisted` adapter: today's manual workflow as a Publisher (spec 03 section 5).

`send` produces a checklist for the person and leaves the target in
`needs_user`; nothing leaves this machine. The person posts on the platform
and then uses "mark as published" (with the post's link). `verify` re-reads
the post from content imported by sync, so `verified` still means "seen on
the platform" (invariant 3). It needs no credentials, is always available and
never fails.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from sqlalchemy import or_, select

from runway.db.base import Database
from runway.db.models import PlatformContent
from runway.platforms.post_settings import parse_settings, privacy_in_words
from runway.platforms.publisher import (
    Issue,
    PollResult,
    Progress,
    PublishContext,
    ReconcileResult,
    SendResult,
    VerifyResult,
)
from runway.posts.types import TargetPostType

_STUDIO = {
    "youtube": "Open YouTube Studio and choose Create, then Upload videos",
    "instagram": "Open Instagram and start a new post",
    "tiktok": "Open TikTok and tap +",
}


def _when(planned_at: datetime | None, timezone: str) -> str:
    if planned_at is None:
        return "now"
    from zoneinfo import ZoneInfo

    instant = planned_at if planned_at.tzinfo else planned_at.replace(tzinfo=UTC)
    local = instant.astimezone(ZoneInfo(timezone))
    return local.strftime("%Y-%m-%d %H:%M %Z")


def _yes_no(value: object) -> str:
    return "Yes" if value is True else "No" if value is False else "Not chosen"


class AssistedPublisher:
    name = "assisted"
    kind = "assisted"
    platform = None

    def __init__(self, database: Database) -> None:
        self.database = database

    def supports(self, post_type: str) -> bool:
        return post_type in {member.value for member in TargetPostType}

    def preflight(self, ctx: PublishContext) -> list[Issue]:
        return []

    def checklist(self, ctx: PublishContext) -> tuple[str, ...]:
        post_type = TargetPostType(ctx.post_type)
        settings = parse_settings(post_type, {})
        with contextlib.suppress(ValueError):
            settings = parse_settings(post_type, ctx.settings)
        steps: list[str] = [_STUDIO[ctx.platform.value]]
        if ctx.account is not None:
            handle = ctx.account.handle or ctx.account.external_account_id
            steps.append(f"Check you are signed in as {handle}")
        for media in ctx.media:
            label = media.original_filename or f"media {media.position + 1}"
            steps.append(f"Add {label}")
        values = dict(ctx.settings)
        if post_type in {TargetPostType.YOUTUBE_VIDEO, TargetPostType.YOUTUBE_SHORT}:
            steps.append(f"Title: {values.get('title') or ctx.title}")
            steps.append("Description: paste the caption")
            steps.append(f"Visibility: {privacy_in_words(post_type, settings)}")
            steps.append(f"Made for kids: {_yes_no(values.get('made_for_kids'))}")
            steps.append(
                f"Paid product placement: {_yes_no(values.get('has_paid_product_placement'))}"
            )
            steps.append(
                f"Altered or synthetic content: {_yes_no(values.get('contains_synthetic_media'))}"
            )
        elif post_type is TargetPostType.TIKTOK_VIDEO:
            steps.append("Caption: paste the caption")
            steps.append(f"Who can view: {privacy_in_words(post_type, settings)}")
            steps.append(
                "Comments / Duet / Stitch: "
                + ", ".join(
                    f"{label} {'on' if values.get(key) else 'off'}"
                    for label, key in (
                        ("comments", "allow_comment"),
                        ("duet", "allow_duet"),
                        ("stitch", "allow_stitch"),
                    )
                )
            )
            steps.append(f"AI-generated label: {_yes_no(values.get('is_aigc'))}")
        elif post_type is not TargetPostType.INSTAGRAM_STORY:
            steps.append("Caption: paste the caption")
            if "is_ai_generated" in values:
                steps.append(f"AI label: {_yes_no(values.get('is_ai_generated'))}")
        steps.append(f"Post it {_when(ctx.planned_at, ctx.timezone)}")
        steps.append("Come back to Runway and mark it as published with the post's link")
        return tuple(steps)

    def send(self, ctx: PublishContext, progress: Progress) -> SendResult:
        steps = self.checklist(ctx)
        progress.report(1, 1, stage="checklist ready")
        return SendResult(
            status="needs_user",
            detail="follow the checklist, then mark the target as published",
            checklist=steps,
            response={"checklist": list(steps)},
        )

    def poll(self, ctx: PublishContext) -> PollResult:
        return PollResult(status="needs_user", detail="waiting for mark as published")

    def _imported(self, ctx: PublishContext) -> PlatformContent | None:
        if ctx.account is None or not (ctx.external_id or ctx.permalink):
            return None
        conditions = []
        if ctx.external_id:
            conditions.append(PlatformContent.external_id == ctx.external_id)
        if ctx.permalink:
            conditions.append(PlatformContent.permalink == ctx.permalink)
        with self.database.session() as session:
            row = session.scalar(
                select(PlatformContent).where(
                    PlatformContent.workspace_id == ctx.workspace_id,
                    PlatformContent.account_id == ctx.account.id,
                    or_(*conditions),
                )
            )
            if row is not None:
                session.expunge(row)
            return row

    def verify(self, ctx: PublishContext) -> VerifyResult:
        row = self._imported(ctx)
        if row is None:
            return VerifyResult(
                verified=None, detail="not seen in imported content yet; the next sync checks"
            )
        return VerifyResult(
            verified=True,
            permalink=row.permalink or ctx.permalink,
            detail="found in content imported from the platform",
        )

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        row = self._imported(ctx)
        checked = ("content imported from the platform by sync",)
        if row is None:
            return ReconcileResult(status="uncertain", checked=checked, detail="not found yet")
        return ReconcileResult(
            status="published",
            external_id=row.external_id,
            permalink=row.permalink,
            checked=checked,
        )
