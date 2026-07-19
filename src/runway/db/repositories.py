from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from runway.db.models import AuditEvent, Channel


def get_channel(session: Session, handle: str) -> Channel:
    channel = session.scalar(select(Channel).where(Channel.handle == handle))
    if channel is None:
        raise LookupError(f"channel @{handle} has not been initialized")
    return channel


def audit(
    session: Session,
    event_type: str,
    entity_type: str,
    entity_id: int | None = None,
    details: dict[str, Any] | None = None,
) -> AuditEvent:
    event = AuditEvent(
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        details_json=json.dumps(details or {}, sort_keys=True, default=str),
    )
    session.add(event)
    return event
