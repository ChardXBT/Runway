from __future__ import annotations

import json

from sqlalchemy import and_, desc, func, not_, or_, select
from sqlalchemy.sql.elements import ColumnElement

from runway.db.base import Database
from runway.db.models import AuditEvent

_CATEGORY_MARKERS: dict[str, tuple[str, ...]] = {
    "publishing": ("youtube", "publish", "lineup", "schedule"),
    "decisions": ("caption", "feedback", "approved", "rejected", "image_replaced"),
    "intelligence": (
        "intelligence",
        "representation",
        "retrieval",
        "profile",
        "diversity",
        "annotation",
    ),
    "data": ("capture", "catalog", "media", "discovery", "search"),
    "settings": ("settings", "policy"),
}


class AuditService:
    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _category_clause(category: str) -> ColumnElement[bool] | None:
        if category == "all":
            return None
        marker_clauses = {
            name: or_(*(AuditEvent.event_type.ilike(f"%{marker}%") for marker in markers))
            for name, markers in _CATEGORY_MARKERS.items()
        }
        ordered = list(_CATEGORY_MARKERS)
        if category == "other":
            return not_(or_(*marker_clauses.values()))
        if category not in marker_clauses:
            raise ValueError(f"unsupported activity category: {category}")
        index = ordered.index(category)
        higher_priority = [marker_clauses[name] for name in ordered[:index]]
        if not higher_priority:
            return marker_clauses[category]
        return and_(marker_clauses[category], not_(or_(*higher_priority)))

    def list_events(
        self,
        *,
        event_type: str | None = None,
        entity_type: str | None = None,
        category: str = "all",
        limit: int = 200,
        offset: int = 0,
    ) -> list[dict[str, object]]:
        with self.database.session() as session:
            statement = select(AuditEvent).order_by(
                desc(func.julianday(AuditEvent.created_at)), desc(AuditEvent.id)
            )
            if event_type:
                statement = statement.where(AuditEvent.event_type == event_type)
            if entity_type:
                statement = statement.where(AuditEvent.entity_type == entity_type)
            category_clause = self._category_clause(category)
            if category_clause is not None:
                statement = statement.where(category_clause)
            events = session.scalars(statement.offset(offset).limit(limit)).all()
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
