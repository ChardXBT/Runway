from __future__ import annotations

import json
from typing import Literal

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CommentRecord,
    ConnectedAccount,
    PlatformAction,
    PlatformContent,
    ReplyDraft,
    utcnow,
)
from runway.platforms.base import ProviderError, iso_utc, redact_payload
from runway.platforms.capabilities import (
    Capability,
    Platform,
    capability_matrix,
    capability_ready,
)
from runway.platforms.content import content_view
from runway.platforms.registry import ProviderRegistry

CommentStatus = Literal["new", "read", "answered", "escalated", "ignored"]
COMMENT_STATUSES = ("new", "read", "answered", "escalated", "ignored")
UNANSWERED_STATUSES = ("new", "read", "escalated")


def _unanswered_clause() -> ColumnElement[bool]:
    return and_(
        CommentRecord.has_owner_reply.is_(False),
        CommentRecord.status.in_(UNANSWERED_STATUSES),
    )


class InboxReplyForbidden(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def comment_view(comment: CommentRecord, draft_text: str | None = None) -> dict[str, object]:
    return {
        "id": comment.id,
        "account_id": comment.account_id,
        "platform": comment.platform,
        "external_id": comment.external_id,
        "content_id": comment.content_id,
        "parent_external_id": comment.parent_external_id,
        "author_name": comment.author_name,
        "author_external_id": comment.author_external_id,
        "author_avatar_url": comment.author_avatar_url,
        "author_is_owner": comment.author_is_owner,
        "text": comment.text,
        "like_count": comment.like_count,
        "reply_count": comment.reply_count,
        "published_at": iso_utc(comment.published_at),
        "status": comment.status,
        "has_owner_reply": comment.has_owner_reply,
        "permalink": comment.permalink,
        "draft": draft_text,
    }


class InboxService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        registry: ProviderRegistry | None = None,
    ) -> None:
        self.database = database
        self.settings = settings
        self.registry = registry

    def list_comments(
        self,
        *,
        platform: str | None = None,
        account_id: int | None = None,
        status: str | None = None,
        unanswered: bool = False,
        content_id: int | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, object]:
        with self.database.session() as session:
            statement = select(CommentRecord).where(CommentRecord.parent_external_id.is_(None))
            count_statement = (
                select(func.count())
                .select_from(CommentRecord)
                .where(CommentRecord.parent_external_id.is_(None))
            )
            if platform:
                statement = statement.where(CommentRecord.platform == platform)
                count_statement = count_statement.where(CommentRecord.platform == platform)
            if account_id is not None:
                statement = statement.where(CommentRecord.account_id == account_id)
                count_statement = count_statement.where(CommentRecord.account_id == account_id)
            if status:
                statement = statement.where(CommentRecord.status == status)
                count_statement = count_statement.where(CommentRecord.status == status)
            if unanswered:
                clause = _unanswered_clause()
                statement = statement.where(clause)
                count_statement = count_statement.where(clause)
            if content_id is not None:
                statement = statement.where(CommentRecord.content_id == content_id)
                count_statement = count_statement.where(CommentRecord.content_id == content_id)
            if query:
                pattern = f"%{query}%"
                clause = or_(
                    CommentRecord.text.like(pattern),
                    CommentRecord.author_name.like(pattern),
                )
                statement = statement.where(clause)
                count_statement = count_statement.where(clause)
            statement = statement.order_by(
                CommentRecord.published_at.desc(), CommentRecord.id.desc()
            )
            total = session.scalar(count_statement) or 0
            comments = session.scalars(statement.limit(limit).offset(offset)).all()
            drafts = {
                draft.comment_id: draft.text
                for draft in session.scalars(
                    select(ReplyDraft).where(ReplyDraft.comment_id.in_([c.id for c in comments]))
                ).all()
            }
            unanswered_count = session.scalar(
                select(func.count())
                .select_from(CommentRecord)
                .where(
                    CommentRecord.parent_external_id.is_(None),
                    _unanswered_clause(),
                )
            )
            new_count = session.scalar(
                select(func.count())
                .select_from(CommentRecord)
                .where(
                    CommentRecord.parent_external_id.is_(None),
                    CommentRecord.status == "new",
                )
            )
            return {
                "items": [comment_view(comment, drafts.get(comment.id)) for comment in comments],
                "total": total,
                "counts": {"unanswered": unanswered_count or 0, "new": new_count or 0},
            }

    def _reply_capability(
        self,
        account: ConnectedAccount,
        platform: Platform,
        permalink: str | None = None,
    ) -> dict[str, object]:
        entry = next(
            entry
            for entry in capability_matrix(
                platform,
                engage_scopes=self.settings.youtube_engage_scopes,
                replies_enabled=self.settings.inbox_replies_enabled,
            )
            if entry.capability is Capability.comments_reply
        )
        granted = json.loads(account.granted_scopes_json or "[]")
        ready = self.settings.inbox_replies_enabled and capability_ready(
            entry, account_status=account.status, granted_scopes=granted
        )
        handoff = permalink
        if handoff is None:
            if platform is Platform.youtube and account.external_account_id:
                handoff = "https://www.youtube.com/comments"
            elif platform is Platform.instagram:
                handoff = "https://www.instagram.com/"
            elif platform is Platform.tiktok:
                handoff = "https://www.tiktok.com/inbox"
        return {
            "ready": ready,
            "state": entry.state,
            "detail": entry.detail,
            "handoff_url": handoff,
        }

    def get_comment(self, comment_id: int) -> dict[str, object] | None:
        with self.database.session() as session:
            comment = session.get(CommentRecord, comment_id)
            if comment is None:
                return None
            account = session.get(ConnectedAccount, comment.account_id)
            platform = Platform(comment.platform)
            draft = session.scalar(select(ReplyDraft).where(ReplyDraft.comment_id == comment_id))
            replies = session.scalars(
                select(CommentRecord)
                .where(
                    CommentRecord.account_id == comment.account_id,
                    CommentRecord.parent_external_id == comment.external_id,
                )
                .order_by(CommentRecord.published_at.asc())
            ).all()
            content = (
                session.get(PlatformContent, comment.content_id)
                if comment.content_id is not None
                else None
            )
            return {
                "comment": comment_view(comment, draft.text if draft else None),
                "replies": [comment_view(reply) for reply in replies],
                "content": content_view(content) if content else None,
                "draft": draft.text if draft else None,
                "reply_capability": (
                    self._reply_capability(
                        account,
                        platform,
                        permalink=comment.permalink or (content.permalink if content else None),
                    )
                    if account is not None
                    else {
                        "ready": False,
                        "state": "unsupported",
                        "detail": "account not found",
                        "handoff_url": None,
                    }
                ),
            }

    def set_status(self, comment_id: int, status: str) -> dict[str, object]:
        if status not in COMMENT_STATUSES:
            raise LookupError(f"invalid status {status!r}")
        with self.database.session() as session:
            comment = session.get(CommentRecord, comment_id)
            if comment is None:
                raise LookupError("comment not found")
            comment.status = status
            session.flush()
            return comment_view(comment)

    def set_draft(self, comment_id: int, text: str) -> dict[str, object]:
        with self.database.session() as session:
            if session.get(CommentRecord, comment_id) is None:
                raise LookupError("comment not found")
            draft = session.scalar(select(ReplyDraft).where(ReplyDraft.comment_id == comment_id))
            if draft is None:
                draft = ReplyDraft(comment_id=comment_id, text=text)
                session.add(draft)
            else:
                draft.text = text
            session.flush()
            return {"comment_id": comment_id, "text": draft.text}

    def delete_draft(self, comment_id: int) -> dict[str, object]:
        with self.database.session() as session:
            draft = session.scalar(select(ReplyDraft).where(ReplyDraft.comment_id == comment_id))
            if draft is not None:
                session.delete(draft)
            return {"comment_id": comment_id, "text": None}

    def reply(self, comment_id: int, text: str) -> dict[str, object]:
        if self.registry is None:
            raise InboxReplyForbidden("reply service is unavailable")
        registry = self.registry
        with self.database.session() as session:
            comment = session.get(CommentRecord, comment_id)
            if comment is None:
                raise LookupError("comment not found")
            account = session.get(ConnectedAccount, comment.account_id)
            if account is None:
                raise InboxReplyForbidden("account not found")
            platform = Platform(comment.platform)
            linked_content = (
                session.get(PlatformContent, comment.content_id)
                if comment.content_id is not None
                else None
            )
            capability = self._reply_capability(
                account,
                platform,
                permalink=comment.permalink
                or (linked_content.permalink if linked_content else None),
            )
            if not capability["ready"]:
                raise InboxReplyForbidden(str(capability["detail"]))
            action = PlatformAction(
                account_id=account.id,
                action_type="comment_reply",
                target_type="comment",
                target_id=comment.id,
                state="proposed",
                request_json=json.dumps(redact_payload({"text": text})),
            )
            session.add(action)
            session.flush()
            action_id = action.id
            account_token_ref = account.token_ref
            comment_external_id = comment.external_id
            account_id = account.id
        if not account_token_ref:
            raise InboxReplyForbidden("account has no stored token")
        token = registry.token_store.load(account_token_ref)
        if token is None:
            raise InboxReplyForbidden("account token is missing")
        provider = registry.provider(platform)

        def _action(session: Session) -> PlatformAction:
            record = session.get(PlatformAction, action_id)
            assert record is not None
            return record

        with self.database.session() as session:
            record = _action(session)
            record.state = "attempted"
            record.attempted_at = utcnow()
        try:
            result = provider.reply_to_comment(
                token, comment_external_id=comment_external_id, text=text
            )
        except ProviderError as exc:
            with self.database.session() as session:
                record = _action(session)
                record.state = "failed"
                record.error = exc.detail
                record.completed_at = utcnow()
            raise
        with self.database.session() as session:
            action = _action(session)
            action.state = "completed" if result.verified else "uncertain"
            action.external_result_id = result.external_id
            action.response_json = json.dumps(
                {"external_id": result.external_id, "verified": result.verified}
            )
            action.completed_at = utcnow()
            if result.verified:
                action.verified_at = utcnow()
            comment = session.get(CommentRecord, comment_id)
            if comment is not None:
                comment.status = "answered"
                comment.has_owner_reply = True
            return {
                "id": action.id,
                "account_id": account_id,
                "action_type": action.action_type,
                "state": action.state,
                "external_result_id": action.external_result_id,
                "verified": result.verified,
                "detail": result.detail,
            }
