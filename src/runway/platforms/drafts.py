from __future__ import annotations

import json
import re
from datetime import datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    ConnectedAccount,
    ContentDraft,
    DraftTarget,
    PlatformContent,
    utcnow,
)
from runway.platforms.base import iso_utc
from runway.platforms.capabilities import (
    Capability,
    Platform,
    capability_matrix,
)


def _publish_capability(
    settings: Settings, platform: str, account_status: str | None
) -> dict[str, object]:
    entry = next(
        entry
        for entry in capability_matrix(
            Platform(platform),
            engage_scopes=settings.youtube_engage_scopes,
            replies_enabled=settings.inbox_replies_enabled,
        )
        if entry.capability is Capability.publishing
    )
    return {"ready": False, "state": entry.state, "detail": entry.detail}


_YOUTUBE_ID_PATTERNS = (
    re.compile(r"[?&]v=([A-Za-z0-9_-]{6,})"),
    re.compile(r"/shorts/([A-Za-z0-9_-]{6,})"),
    re.compile(r"youtu\.be/([A-Za-z0-9_-]{6,})"),
)
_TIKTOK_ID_PATTERN = re.compile(r"/video/(\d{6,})")


def external_id_candidates(platform: str, url: str) -> list[str]:
    candidates = [url]
    if platform == "youtube":
        for pattern in _YOUTUBE_ID_PATTERNS:
            match = pattern.search(url)
            if match:
                candidates.append(match.group(1))
                break
    elif platform == "tiktok":
        match = _TIKTOK_ID_PATTERN.search(url)
        if match:
            candidates.append(match.group(1))
    return candidates


class DraftService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    def _view(self, session: Session, draft: ContentDraft) -> dict[str, object]:
        targets = session.scalars(
            select(DraftTarget)
            .where(DraftTarget.draft_id == draft.id)
            .order_by(DraftTarget.id.asc())
        ).all()
        target_views: list[dict[str, object]] = []
        for target in targets:
            status = None
            if target.account_id is not None:
                account = session.get(ConnectedAccount, target.account_id)
                status = account.status if account else None
            target_views.append(
                {
                    "id": target.id,
                    "platform": target.platform,
                    "account_id": target.account_id,
                    "caption_override": target.caption_override,
                    "settings": json.loads(target.settings_json or "{}"),
                    "status": target.status,
                    "published_content_id": target.published_content_id,
                    "published_url": target.published_url,
                    "published_at": (iso_utc(target.published_at)),
                    "error": target.error,
                    "publish_capability": _publish_capability(
                        self.settings, target.platform, status
                    ),
                }
            )
        return {
            "id": draft.id,
            "title": draft.title,
            "body": draft.body,
            "media_path": draft.media_path,
            "media_kind": draft.media_kind,
            "scheduled_for": (iso_utc(draft.scheduled_for)),
            "status": draft.status,
            "notes": draft.notes,
            "created_at": iso_utc(draft.created_at),
            "updated_at": iso_utc(draft.updated_at),
            "targets": target_views,
        }

    def list_drafts(self, *, status: str | None = None) -> list[dict[str, object]]:
        with self.database.session() as session:
            statement = select(ContentDraft).where(ContentDraft.status != "archived")
            if status:
                statement = select(ContentDraft).where(ContentDraft.status == status)
            drafts = session.scalars(statement.order_by(ContentDraft.id.desc())).all()
            return [self._view(session, draft) for draft in drafts]

    def get_draft(self, draft_id: int) -> dict[str, object] | None:
        with self.database.session() as session:
            draft = session.get(ContentDraft, draft_id)
            if draft is None:
                return None
            return self._view(session, draft)

    def create_draft(
        self,
        *,
        title: str,
        body: str = "",
        scheduled_for: datetime | None = None,
        notes: str | None = None,
        targets: list[dict[str, object]] | None = None,
    ) -> dict[str, object]:
        with self.database.session() as session:
            draft = ContentDraft(
                title=title,
                body=body,
                scheduled_for=scheduled_for,
                notes=notes,
                status="scheduled" if scheduled_for else "draft",
            )
            session.add(draft)
            session.flush()
            for target in targets or []:
                raw_account_id = target.get("account_id")
                account_id = raw_account_id if isinstance(raw_account_id, int) else None
                if account_id is None:
                    bound = session.scalars(
                        select(ConnectedAccount).where(
                            ConnectedAccount.platform == str(target["platform"]),
                            ConnectedAccount.status == "connected",
                        )
                    ).all()
                    if len(bound) == 1:
                        account_id = bound[0].id
                session.add(
                    DraftTarget(
                        draft_id=draft.id,
                        platform=str(target["platform"]),
                        account_id=account_id,
                        caption_override=(
                            str(target["caption_override"])
                            if target.get("caption_override")
                            else None
                        ),
                    )
                )
            session.flush()
            return self._view(session, draft)

    def update_draft(
        self,
        draft_id: int,
        *,
        title: str | None = None,
        body: str | None = None,
        scheduled_for: datetime | None = None,
        clear_schedule: bool = False,
        notes: str | None = None,
        status: str | None = None,
    ) -> dict[str, object]:
        with self.database.session() as session:
            draft = session.get(ContentDraft, draft_id)
            if draft is None:
                raise LookupError("draft not found")
            if title is not None:
                draft.title = title
            if body is not None:
                draft.body = body
            if clear_schedule:
                draft.scheduled_for = None
            elif scheduled_for is not None:
                draft.scheduled_for = scheduled_for
            if notes is not None:
                draft.notes = notes
            if status is not None:
                draft.status = status
            elif draft.scheduled_for is not None and draft.status == "draft":
                draft.status = "scheduled"
            session.flush()
            return self._view(session, draft)

    def archive_draft(self, draft_id: int) -> dict[str, object]:
        with self.database.session() as session:
            draft = session.get(ContentDraft, draft_id)
            if draft is None:
                raise LookupError("draft not found")
            draft.status = "archived"
            session.flush()
            return {"id": draft.id, "status": "archived"}

    def _recompute_status(self, session: Session, draft: ContentDraft) -> None:
        targets = session.scalars(select(DraftTarget).where(DraftTarget.draft_id == draft.id)).all()
        if not targets:
            return
        published = sum(1 for target in targets if target.status == "published")
        if published == len(targets):
            draft.status = "published"
        elif published > 0:
            draft.status = "partially_published"

    def mark_published(
        self, draft_id: int, target_id: int, published_url: str
    ) -> dict[str, object]:
        with self.database.session() as session:
            draft = session.get(ContentDraft, draft_id)
            if draft is None:
                raise LookupError("draft not found")
            target = session.get(DraftTarget, target_id)
            if target is None or target.draft_id != draft_id:
                raise LookupError("target not found")
            candidates = external_id_candidates(target.platform, published_url)
            match = session.scalar(
                select(PlatformContent).where(
                    or_(
                        PlatformContent.permalink == published_url,
                        PlatformContent.external_id.in_(candidates),
                    ),
                    PlatformContent.platform == target.platform,
                )
            )
            target.status = "published"
            target.published_url = published_url
            target.published_at = utcnow()
            target.published_content_id = match.id if match else None
            self._recompute_status(session, draft)
            session.flush()
            return self._view(session, draft)
