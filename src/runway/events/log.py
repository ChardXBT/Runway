"""Writing and reading the `events` table."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from runway.db.models import EventRecord, utcnow
from runway.events.types import EVENT_TYPES

EVENT_RETENTION = timedelta(days=7)
# Payloads are ids and small summaries; clients refetch what they show.
MAX_PAYLOAD_BYTES = 4096


class UnknownEventType(ValueError):
    pass


class EventPayloadTooLarge(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    workspace_id: int
    type: str
    subject_type: str
    subject_id: str | None
    payload: dict[str, Any]
    created_at: datetime

    @classmethod
    def of(cls, row: EventRecord) -> Event:
        payload = json.loads(row.payload_json or "{}")
        created = row.created_at
        return cls(
            id=row.id,
            workspace_id=row.workspace_id,
            type=row.type,
            subject_type=row.subject_type,
            subject_id=row.subject_id,
            payload=payload if isinstance(payload, dict) else {},
            created_at=created if created.tzinfo is not None else created.replace(tzinfo=UTC),
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "type": self.type,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "payload": self.payload,
            "created_at": self.created_at.isoformat(),
        }


def record_event(
    session: Session,
    workspace_id: int,
    type: str,
    subject_type: str,
    subject_id: str | int | None,
    payload: dict[str, Any] | None = None,
) -> EventRecord:
    """Add an event to the caller's transaction (flushed, never committed here).

    Writing the event in the same transaction as the change it describes means
    a rolled-back change leaves no event, and a committed one always has one.
    """
    if type not in EVENT_TYPES:
        raise UnknownEventType(f"{type!r} is not declared in runway.events.types")
    encoded = json.dumps(payload or {}, sort_keys=True, separators=(",", ":"), default=str)
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise EventPayloadTooLarge(
            f"event payload is over {MAX_PAYLOAD_BYTES} bytes; send ids and a short summary"
        )
    row = EventRecord(
        workspace_id=workspace_id,
        type=type,
        subject_type=subject_type,
        subject_id=None if subject_id is None else str(subject_id),
        payload_json=encoded,
    )
    session.add(row)
    session.flush()
    return row


def events_after(session: Session, workspace_id: int, after_id: int, limit: int) -> list[Event]:
    rows = session.scalars(
        select(EventRecord)
        .where(EventRecord.workspace_id == workspace_id, EventRecord.id > after_id)
        .order_by(EventRecord.id)
        .limit(limit)
    ).all()
    return [Event.of(row) for row in rows]


def latest_event_id(session: Session) -> int:
    """The newest id in the whole table (ids are global, not per workspace)."""
    return int(session.scalar(select(func.max(EventRecord.id))) or 0)


def resume_is_complete(
    session: Session, workspace_id: int, last_event_id: int, *, now: datetime | None = None
) -> bool:
    """True when every event after `last_event_id` is still in the table.

    False (the client must resync) when that id is unknown, belongs to another
    workspace, was purged, or is older than the retention window.
    """
    if last_event_id <= 0 or last_event_id > latest_event_id(session):
        return False
    row = session.get(EventRecord, last_event_id)
    if row is None or row.workspace_id != workspace_id:
        return False
    created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=UTC)
    return created >= (now or utcnow()) - EVENT_RETENTION


def purge_expired_events(session: Session, *, now: datetime | None = None) -> int:
    cutoff = (now or utcnow()) - EVENT_RETENTION
    result = session.execute(delete(EventRecord).where(EventRecord.created_at < cutoff))
    return int(getattr(result, "rowcount", 0) or 0)
