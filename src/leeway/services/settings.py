from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from leeway.config import Settings
from leeway.db.base import Database
from leeway.db.models import BlockedSource
from leeway.db.repositories import audit, get_channel


class SettingsService:
    editable = {
        "name",
        "handle",
        "timezone",
        "default_post_time",
        "planning_horizon_days",
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
                    "planning_horizon_days": channel.planning_horizon_days,
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
        for key in ("planning_horizon_days", "duplicate_window_days"):
            if key in fields and int(fields[key]) < 1:
                raise ValueError(f"{key} must be positive")
        with self.database.session() as session:
            channel = get_channel(session, self.settings.channel_handle)
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
