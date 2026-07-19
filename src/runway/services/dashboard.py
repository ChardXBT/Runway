from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select

from runway.db.base import Database
from runway.db.models import CaptureRun, GenerationRun, Post, Proposal, StyleProfile
from runway.db.repositories import get_channel


class DashboardService:
    def __init__(self, database: Database):
        self.database = database

    def summary(self) -> dict[str, object]:
        with self.database.session() as session:
            channel = get_channel(
                session,
                self.database.settings.channel_handle,
            )
            proposal_counts: dict[str, int] = {
                status: count
                for status, count in session.execute(
                    select(Proposal.status, func.count(Proposal.id))
                    .where(Proposal.channel_id == channel.id)
                    .group_by(Proposal.status)
                )
            }
            scheduled_count = (
                session.scalar(
                    select(func.count(Proposal.id)).where(
                        Proposal.channel_id == channel.id,
                        Proposal.scheduled_publish_at.is_not(None)
                    )
                )
                or 0
            )
            last_capture = session.scalar(
                select(CaptureRun)
                .where(CaptureRun.channel_id == channel.id)
                .order_by(CaptureRun.started_at.desc())
                .limit(1)
            )
            last_profile = session.scalar(
                select(StyleProfile)
                .where(StyleProfile.channel_id == channel.id)
                .order_by(StyleProfile.version.desc())
                .limit(1)
            )
            last_generation = session.scalar(
                select(GenerationRun)
                .where(GenerationRun.channel_id == channel.id)
                .order_by(GenerationRun.started_at.desc())
                .limit(1)
            )
            catalogue_count = (
                session.scalar(
                    select(func.count(Post.id)).where(
                        Post.channel_id == channel.id
                    )
                )
                or 0
            )
            return {
                "catalogue_count": catalogue_count,
                "queue_coverage": scheduled_count,
                "needs_review": proposal_counts.get("needs_review", 0),
                "approved": proposal_counts.get("approved", 0),
                "gaps": 0,
                "last_capture": _run_summary(last_capture),
                "active_profile_version": last_profile.version if last_profile else None,
                "last_generation": _run_summary(last_generation),
            }


def _run_summary(run: object | None) -> dict[str, object] | None:
    if run is None:
        return None
    started_at = getattr(run, "started_at", None)
    return {
        "id": getattr(run, "id", None),
        "status": getattr(run, "status", None),
        "started_at": started_at.isoformat() if isinstance(started_at, datetime) else started_at,
    }
