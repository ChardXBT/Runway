from __future__ import annotations

import json
from typing import Any, Literal

from sqlalchemy import ColumnElement, asc, desc, func, or_, select
from sqlalchemy.orm import InstrumentedAttribute

from runway.db.base import Database
from runway.db.models import CommentRecord, ContentGroup, ContentMetricSnapshot, PlatformContent
from runway.platforms.base import iso_utc

SortField = Literal["published_at", "view_count", "like_count", "comment_count"]

_SORT_COLUMNS: dict[str, InstrumentedAttribute[Any]] = {
    "published_at": PlatformContent.published_at,
    "view_count": PlatformContent.view_count,
    "like_count": PlatformContent.like_count,
    "comment_count": PlatformContent.comment_count,
}


def content_view(content: PlatformContent) -> dict[str, object]:
    try:
        metrics = json.loads(content.metrics_json or "{}")
    except json.JSONDecodeError:
        metrics = {}
    return {
        "id": content.id,
        "account_id": content.account_id,
        "platform": content.platform,
        "external_id": content.external_id,
        "content_type": content.content_type,
        "title": content.title,
        "caption": content.caption,
        "permalink": content.permalink,
        "thumbnail_url": content.thumbnail_url,
        "published_at": iso_utc(content.published_at),
        "duration_seconds": content.duration_seconds,
        "privacy_status": content.privacy_status,
        "view_count": content.view_count,
        "like_count": content.like_count,
        "comment_count": content.comment_count,
        "share_count": content.share_count,
        "metrics": metrics,
        "metrics_fetched_at": (iso_utc(content.metrics_fetched_at)),
        "group_id": content.content_group_id,
        "first_seen_at": iso_utc(content.first_seen_at),
        "last_seen_at": iso_utc(content.last_seen_at),
    }


class ContentService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def list_items(
        self,
        *,
        platform: str | None = None,
        account_id: int | None = None,
        content_type: str | None = None,
        query: str | None = None,
        sort: SortField = "published_at",
        order: Literal["asc", "desc"] = "desc",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        def filters(
            *,
            include_platform: bool = True,
            include_type: bool = True,
        ) -> list[ColumnElement[bool]]:
            clauses: list[ColumnElement[bool]] = []
            if include_platform and platform:
                clauses.append(PlatformContent.platform == platform)
            if account_id is not None:
                clauses.append(PlatformContent.account_id == account_id)
            if include_type and content_type:
                clauses.append(PlatformContent.content_type == content_type)
            if query:
                pattern = f"%{query}%"
                clauses.append(
                    or_(
                        PlatformContent.title.like(pattern),
                        PlatformContent.caption.like(pattern),
                    )
                )
            return clauses

        with self.database.session() as session:
            statement = select(PlatformContent).where(*filters())
            count_statement = select(func.count()).select_from(PlatformContent).where(*filters())
            column = _SORT_COLUMNS.get(sort, PlatformContent.published_at)
            statement = statement.order_by(
                desc(column) if order == "desc" else asc(column),
                desc(PlatformContent.id),
            )
            total = session.scalar(count_statement) or 0
            items = session.scalars(statement.limit(limit).offset(offset)).all()
            by_platform = {
                row[0]: row[1]
                for row in session.execute(
                    select(PlatformContent.platform, func.count())
                    .where(*filters(include_platform=False))
                    .group_by(PlatformContent.platform)
                )
            }
            by_type = {
                row[0]: row[1]
                for row in session.execute(
                    select(PlatformContent.content_type, func.count())
                    .where(*filters(include_type=False))
                    .group_by(PlatformContent.content_type)
                )
            }
            return {
                "items": [content_view(item) for item in items],
                "total": total,
                "counts": {"by_platform": by_platform, "by_type": by_type},
            }

    def get_item(self, content_id: int) -> dict[str, object] | None:
        with self.database.session() as session:
            content = session.get(PlatformContent, content_id)
            if content is None:
                return None
            view = content_view(content)
            snapshots = session.scalars(
                select(ContentMetricSnapshot)
                .where(ContentMetricSnapshot.content_id == content_id)
                .order_by(ContentMetricSnapshot.captured_at.asc())
            ).all()
            view["metric_history"] = [
                {
                    "captured_at": iso_utc(snapshot.captured_at),
                    "metrics": json.loads(snapshot.metrics_json or "{}"),
                }
                for snapshot in snapshots
            ]
            view["comment_count_local"] = session.scalar(
                select(func.count())
                .select_from(CommentRecord)
                .where(CommentRecord.content_id == content_id)
            )
            if content.content_group_id is not None:
                group = session.get(ContentGroup, content.content_group_id)
                members = session.scalars(
                    select(PlatformContent).where(
                        PlatformContent.content_group_id == content.content_group_id,
                        PlatformContent.id != content_id,
                    )
                ).all()
                view["group"] = {
                    "id": content.content_group_id,
                    "title": group.title if group else None,
                    "members": [
                        {
                            "id": member.id,
                            "external_id": member.external_id,
                            "title": member.title,
                            "platform": member.platform,
                        }
                        for member in members
                    ],
                }
            else:
                view["group"] = None
            return view

    def create_group(self, title: str, content_ids: list[int]) -> dict[str, object]:
        with self.database.session() as session:
            group = ContentGroup(title=title)
            session.add(group)
            session.flush()
            linked = session.scalars(
                select(PlatformContent).where(PlatformContent.id.in_(content_ids))
            ).all()
            for content in linked:
                content.content_group_id = group.id
            return {
                "id": group.id,
                "title": group.title,
                "content_ids": [content.id for content in linked],
            }

    def set_group(self, content_id: int, group_id: int | None) -> dict[str, object]:
        with self.database.session() as session:
            content = session.get(PlatformContent, content_id)
            if content is None:
                raise LookupError("content not found")
            if group_id is not None and session.get(ContentGroup, group_id) is None:
                raise LookupError("group not found")
            content.content_group_id = group_id
            session.flush()
            return {"id": content.id, "group_id": group_id}
