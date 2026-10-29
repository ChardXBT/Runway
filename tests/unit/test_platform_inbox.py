from __future__ import annotations

import json

import httpx
import pytest
from sqlalchemy import select

from runway.db.base import Database
from runway.db.models import CommentRecord, ConnectedAccount, PlatformAction, utcnow
from runway.platforms.base import TokenSet
from runway.platforms.capabilities import YOUTUBE_ENGAGE_SCOPE, Platform
from runway.platforms.inbox import InboxService
from runway.platforms.mock import MockPlatformProvider
from runway.platforms.registry import ProviderRegistry


def _account(database: Database) -> ConnectedAccount:
    with database.session() as session:
        account = ConnectedAccount(
            platform="youtube",
            external_account_id="UCx",
            status="connected",
            granted_scopes_json="[]",
        )
        session.add(account)
        session.flush()
        session.expunge(account)
        return account


def _comment(database: Database, account_id: int, external_id: str, **overrides: object) -> int:
    with database.session() as session:
        comment = CommentRecord(
            account_id=account_id,
            platform="youtube",
            external_id=external_id,
            text="hello",
            status=str(overrides.get("status", "new")),
            has_owner_reply=bool(overrides.get("has_owner_reply", False)),
            permalink=overrides.get("permalink"),
            published_at=utcnow(),
        )
        session.add(comment)
        session.flush()
        return comment.id


def test_unanswered_requires_no_owner_reply_and_open_status(database: Database) -> None:
    service = InboxService(database, database.settings)
    account = _account(database)
    # owner replied but status still new -> answered, NOT unanswered
    answered_id = _comment(database, account.id, "c-answered", status="new", has_owner_reply=True)
    # escalated without owner reply -> unanswered
    escalated_id = _comment(
        database, account.id, "c-escalated", status="escalated", has_owner_reply=False
    )
    _comment(database, account.id, "c-ignored", status="ignored")

    result = service.list_comments(unanswered=True)
    ids = {item["id"] for item in result["items"]}
    assert answered_id not in ids
    assert escalated_id in ids
    assert result["counts"]["unanswered"] == 1


def test_handoff_url_prefers_comment_then_content_permalink(
    database: Database,
) -> None:
    from runway.db.models import PlatformContent

    service = InboxService(database, database.settings)
    account = _account(database)
    with database.session() as session:
        content = PlatformContent(
            account_id=account.id,
            platform="youtube",
            external_id="v1",
            content_type="video",
            permalink="https://youtu.be/v1",
        )
        session.add(content)
        session.flush()
        content_id = content.id
    with_permalink = _comment(
        database,
        account.id,
        "c1",
        permalink="https://youtube.com/comment/c1",
        status="new",
    )
    with database.session() as session:
        bare = CommentRecord(
            account_id=account.id,
            platform="youtube",
            external_id="c2",
            text="x",
            status="new",
            content_id=content_id,
        )
        session.add(bare)
        session.flush()
        bare_id = bare.id
    view = service.get_comment(with_permalink)
    assert view is not None
    capability = view["reply_capability"]
    assert capability["handoff_url"] == "https://youtube.com/comment/c1"
    view = service.get_comment(bare_id)
    assert view is not None
    assert view["reply_capability"]["handoff_url"] == "https://youtu.be/v1"


def test_reply_transport_error_marks_action_failed(database: Database) -> None:
    settings = database.settings.model_copy(
        update={"inbox_replies_enabled": True, "youtube_engage_scopes": True}
    )
    registry = ProviderRegistry(database, settings)

    class BrokenReply(MockPlatformProvider):
        def reply_to_comment(self, token, *, comment_external_id, text):
            raise httpx.ConnectError("connection refused")

    registry.set_provider(Platform.youtube, BrokenReply(Platform.youtube, settings))
    service = InboxService(database, settings, registry)

    token_ref = registry.token_store.new_token_ref()
    with database.session() as session:
        account = ConnectedAccount(
            platform="youtube",
            external_account_id="UCx",
            status="connected",
            granted_scopes_json=json.dumps([YOUTUBE_ENGAGE_SCOPE]),
            token_ref=token_ref,
        )
        session.add(account)
        session.flush()
        account_id = account.id
    registry.token_store.store(token_ref, TokenSet(access_token="yt-access"))
    comment_id = _comment(database, account_id, "c1")

    with pytest.raises(httpx.ConnectError):
        service.reply(comment_id, "hello")

    with database.session() as session:
        action = session.scalar(select(PlatformAction))
        assert action is not None
        assert action.state == "failed"
        assert action.error
        assert action.completed_at is not None
