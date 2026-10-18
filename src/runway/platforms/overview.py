from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    AccountMetricDay,
    AccountMetricSnapshot,
    CommentRecord,
    ConnectedAccount,
    ContentDraft,
    PlatformContent,
    Proposal,
    SyncRun,
)
from runway.db.repositories import get_channel
from runway.platforms.base import iso_utc
from runway.platforms.capabilities import Platform
from runway.proposals.service import ProposalService
from runway.services.dashboard import DashboardService

FRESHNESS_STALE_AFTER = timedelta(hours=24)
UNANSWERED_STATUSES = ("new", "read", "escalated")


def _freshness(last_sync_at: datetime | None) -> str:
    if last_sync_at is None:
        return "never"
    value = last_sync_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    if datetime.now(UTC) - value > FRESHNESS_STALE_AFTER:
        return "stale"
    return "fresh"


class OverviewService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    def _configured(self, platform: Platform) -> bool:
        if self.settings.platform_provider == "mock":
            return True
        if platform is Platform.youtube:
            return self.settings.youtube_oauth_configured
        if platform is Platform.instagram:
            return self.settings.instagram_oauth_configured
        return self.settings.tiktok_oauth_configured

    def _platform_state(self, account: ConnectedAccount | None, platform: Platform) -> str:
        if account is None:
            return "not_connected" if self._configured(platform) else "not_configured"
        if account.status == "needs_reauth":
            return "needs_reauth"
        return account.status

    def summary(self, account_views: list[dict[str, object]]) -> dict[str, object]:
        generated_at = datetime.now(UTC)
        with self.database.session() as session:
            accounts = session.scalars(select(ConnectedAccount)).all()
            snapshots = {
                row.id: row
                for row in session.scalars(
                    select(AccountMetricSnapshot).where(
                        AccountMetricSnapshot.id.in_(
                            select(func.max(AccountMetricSnapshot.id)).group_by(
                                AccountMetricSnapshot.account_id
                            )
                        )
                    )
                ).all()
            }
            summaries: list[dict[str, object]] = []
            warnings: list[str] = []
            for platform in Platform:
                platform_accounts = [
                    account for account in accounts if account.platform == platform.value
                ]
                if not platform_accounts:
                    summaries.append(
                        {
                            "platform": platform.value,
                            "account_id": None,
                            "handle": None,
                            "display_name": None,
                            "avatar_url": None,
                            "state": self._platform_state(None, platform),
                            "freshness": "never",
                            "followers_label": (
                                "Subscribers" if platform is Platform.youtube else "Followers"
                            ),
                            "followers": None,
                            "content_count": None,
                            "views_28d": None,
                            "last_sync_at": None,
                            "top_content": [],
                        }
                    )
                    continue
                for account in platform_accounts:
                    state = self._platform_state(account, platform)
                    if account.status == "needs_reauth":
                        warnings.append(
                            f"{platform.value.capitalize()} token needs re-authorization"
                        )
                    views_28d = session.scalar(
                        select(func.sum(AccountMetricDay.value)).where(
                            AccountMetricDay.account_id == account.id,
                            AccountMetricDay.metric == "views",
                            AccountMetricDay.day >= date.today() - timedelta(days=28),
                        )
                    )
                    top_content = session.scalars(
                        select(PlatformContent)
                        .where(PlatformContent.account_id == account.id)
                        .order_by(PlatformContent.view_count.desc())
                        .limit(3)
                    ).all()
                    snapshot = snapshots.get(account.id)
                    summaries.append(
                        {
                            "platform": platform.value,
                            "account_id": account.id,
                            "handle": account.handle,
                            "display_name": account.display_name,
                            "avatar_url": account.avatar_url,
                            "state": state,
                            "freshness": _freshness(account.last_sync_at),
                            "followers_label": (
                                "Subscribers" if platform is Platform.youtube else "Followers"
                            ),
                            "followers": snapshot.followers if snapshot else None,
                            "content_count": session.scalar(
                                select(func.count())
                                .select_from(PlatformContent)
                                .where(PlatformContent.account_id == account.id)
                            ),
                            "views_28d": views_28d,
                            "last_sync_at": (iso_utc(account.last_sync_at)),
                            "top_content": [
                                {
                                    "id": item.id,
                                    "title": item.title,
                                    "caption": item.caption,
                                    "content_type": item.content_type,
                                    "view_count": item.view_count,
                                    "like_count": item.like_count,
                                    "comment_count": item.comment_count,
                                    "published_at": (iso_utc(item.published_at)),
                                    "permalink": item.permalink,
                                }
                                for item in top_content
                            ],
                        }
                    )

            unanswered = session.scalars(
                select(CommentRecord)
                .where(
                    CommentRecord.parent_external_id.is_(None),
                    CommentRecord.has_owner_reply.is_(False),
                    CommentRecord.status.in_(UNANSWERED_STATUSES),
                )
                .order_by(CommentRecord.published_at.desc())
            ).all()
            inbox_by_platform: dict[str, int] = {}
            for comment in unanswered:
                inbox_by_platform[comment.platform] = inbox_by_platform.get(comment.platform, 0) + 1
            latest_comments = session.scalars(
                select(CommentRecord)
                .where(CommentRecord.parent_external_id.is_(None))
                .order_by(CommentRecord.published_at.desc())
                .limit(5)
            ).all()

            draft_counts = {
                status: count
                for status, count in session.execute(
                    select(ContentDraft.status, func.count(ContentDraft.id)).group_by(
                        ContentDraft.status
                    )
                )
            }
            scheduled_drafts = session.scalars(
                select(ContentDraft)
                .where(
                    ContentDraft.scheduled_for.is_not(None),
                    ContentDraft.status != "archived",
                )
                .order_by(ContentDraft.scheduled_for.asc())
                .limit(5)
            ).all()

            recent_rows = session.execute(
                select(SyncRun, ConnectedAccount)
                .join(ConnectedAccount, SyncRun.account_id == ConnectedAccount.id)
                .order_by(SyncRun.started_at.desc())
                .limit(5)
            ).all()

            channel = get_channel(session, self.settings.channel_handle)
            published_count = session.scalar(
                select(func.count())
                .select_from(Proposal)
                .where(
                    Proposal.channel_id == channel.id,
                    Proposal.status == "published",
                )
            )

        queue = ProposalService(self.database, self.settings).queue_status()
        scheduled = queue.get("scheduled", [])
        upcoming: list[dict[str, object]] = []
        now_utc = datetime.now(UTC)
        now_naive = datetime.now()
        for row in scheduled if isinstance(scheduled, list) else []:
            if not isinstance(row, dict):
                continue
            raw = row.get("scheduled_publish_at")
            if not isinstance(raw, str) or not raw:
                continue
            try:
                when = datetime.fromisoformat(raw)
            except ValueError:
                continue
            if when.tzinfo is not None:
                if when.astimezone(UTC) < now_utc:
                    continue
            elif when < now_naive:
                continue
            candidate = row.get("candidate")
            image_url = None
            if isinstance(candidate, dict):
                image_url = candidate.get("preview_url") or candidate.get("original_url")
            upcoming.append(
                {
                    "id": row.get("id"),
                    "final_caption": row.get("final_caption"),
                    "scheduled_publish_at": raw,
                    "status": row.get("status"),
                    "image_url": image_url,
                }
            )
            if len(upcoming) == 5:
                break

        dashboard = DashboardService(self.database).summary()

        return {
            "generated_at": iso_utc(generated_at),
            "provider_mode": self.settings.platform_provider,
            "accounts": account_views,
            "platform_summaries": summaries,
            "inbox": {
                "unanswered_total": len(unanswered),
                "by_platform": inbox_by_platform,
                "latest": [
                    {
                        "id": comment.id,
                        "platform": comment.platform,
                        "author_name": comment.author_name,
                        "text": comment.text[:200],
                        "published_at": (iso_utc(comment.published_at)),
                        "status": comment.status,
                    }
                    for comment in latest_comments
                ],
            },
            "lineup": {
                "next": upcoming,
                "needs_review": dashboard.get("needs_review", 0),
                "in_lineup": dashboard.get("queue_coverage", 0),
                "published_count": published_count or 0,
            },
            "drafts": {
                "scheduled_next": [
                    {
                        "id": draft.id,
                        "title": draft.title,
                        "scheduled_for": (iso_utc(draft.scheduled_for)),
                        "status": draft.status,
                    }
                    for draft in scheduled_drafts
                ],
                "counts": draft_counts,
            },
            "recent_sync_runs": [
                {
                    "id": run.id,
                    "account_id": run.account_id,
                    "platform": account.platform,
                    "handle": account.handle,
                    "status": run.status,
                    "items_fetched": run.items_fetched,
                    "error_summary": run.error_summary,
                    "started_at": iso_utc(run.started_at),
                }
                for run, account in recent_rows
            ],
            "warnings": warnings,
        }
