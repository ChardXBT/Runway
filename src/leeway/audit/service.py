from __future__ import annotations

import json

from sqlalchemy import desc, select

from leeway.db.base import Database
from leeway.db.models import AuditEvent


class AuditService:
    def __init__(self, database: Database):
        self.database = database

    def list_events(
        self,
        *,
        event_type: str | None = None,
        entity_type: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        with self.database.session() as session:
            statement = select(AuditEvent).order_by(
                desc(AuditEvent.created_at), desc(AuditEvent.id)
            )
            if event_type:
                statement = statement.where(AuditEvent.event_type == event_type)
            if entity_type:
                statement = statement.where(AuditEvent.entity_type == entity_type)
            events = session.scalars(statement.limit(limit)).all()
            return [
                {
                    "id": event.id,
                    "event_type": event.event_type,
                    "entity_type": event.entity_type,
                    "entity_id": event.entity_id,
                    "details": json.loads(event.details_json),
                    "created_at": event.created_at.isoformat(),
                }
                for event in events
            ]
