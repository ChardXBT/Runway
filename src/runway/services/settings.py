from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import BlockedSource, Proposal, PublishAttempt
from runway.db.repositories import audit, get_channel
from runway.domain.enums import ProposalStatus


class SettingsService:
    editable = {
        "timezone",
        "default_post_time",
        "duplicate_window_days",
    }

    def __init__(self, database: Database, settings: Settings):
        self.database = database
        self.settings = settings

    def get(self) -> dict[str, object]:
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            blocked = session.scalars(
                select(BlockedSource).order_by(BlockedSource.source_type, BlockedSource.value)
            ).all()
            result = self.settings.public_dict()
            result.update(
                {
                    "channel_name": channel.name,
                    "channel_handle": channel.handle,
                    "timezone": channel.timezone,
                    "default_post_time": channel.default_post_time,
                    "duplicate_window_days": channel.duplicate_window_days,
                    "blocked_sources": [
                        {
                            "id": item.id,
                            "type": item.source_type,
                            "value": item.value,
                            "reason": item.reason,
                        }
                        for item in blocked
                    ],
                }
            )
            return result

    def update(self, fields: dict[str, Any]) -> dict[str, object]:
        unexpected = set(fields) - self.editable
        if unexpected:
            raise ValueError(f"unsupported settings: {', '.join(sorted(unexpected))}")
        if "timezone" in fields:
            ZoneInfo(str(fields["timezone"]))
        if "default_post_time" in fields:
            parts = str(fields["default_post_time"]).split(":")
            if len(parts) != 2 or not all(part.isdigit() for part in parts):
                raise ValueError("default_post_time must be HH:MM")
            hour, minute = (int(part) for part in parts)
            if hour not in range(24) or minute not in range(60):
                raise ValueError("default_post_time is invalid")
        for key in ("duplicate_window_days",):
            if key in fields and int(fields[key]) < 1:
                raise ValueError(f"{key} must be positive")
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
            requested_timezone = str(fields.get("timezone", channel.timezone))
            if requested_timezone != channel.timezone:
                active_statuses = {
                    ProposalStatus.APPROVED.value,
                    ProposalStatus.INTERNALLY_SCHEDULED.value,
                    ProposalStatus.PUBLISHING.value,
                    ProposalStatus.EXTERNALLY_SCHEDULED.value,
                    ProposalStatus.PUBLISH_UNVERIFIED.value,
                    ProposalStatus.PUBLISH_FAILED.value,
                }
                active_proposal = session.scalar(
                    select(Proposal.id)
                    .where(
                        Proposal.channel_id == channel.id,
                        Proposal.status.in_(active_statuses),
                    )
                    .limit(1)
                )
                active_attempt = session.scalar(
                    select(PublishAttempt.id)
                    .join(Proposal, Proposal.id == PublishAttempt.proposal_id)
                    .where(
                        Proposal.channel_id == channel.id,
                        PublishAttempt.status.in_(
                            ["prepared", "queued", "blocked_session", "submitting"]
                        ),
                    )
                    .limit(1)
                )
                if active_proposal is not None or active_attempt is not None:
                    raise ValueError(
                        "timezone cannot change while Lineup or publisher work is active; "
                        "complete or remove those posts first"
                    )
            before = {key: getattr(channel, key) for key in fields}
            for key, value in fields.items():
                setattr(channel, key, value)
            audit(
                session,
                "settings_updated",
                "channel",
                channel.id,
                {"old": before, "new": fields},
            )
        return self.get()
