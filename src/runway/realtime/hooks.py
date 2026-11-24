"""Publish-progress hooks shared by webhook processors (spec 04 section 7).

`on_publish_event` stays deliberately small: this branch has no publish-target
column keyed by the platform's `publish_id` (the publishing engine merges
later), so a `post.publish.*` hint becomes a `target.progress` event on the
account. The caller's transaction owns the write.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from runway.db.models import ConnectedAccount
from runway.events.log import record_event
from runway.events.types import TARGET_PROGRESS


def on_publish_event(
    session: Session, account_id: int, publish_id: str | None, event_type: str
) -> None:
    """Record a TikTok publish-progress hint for the account."""
    account = session.get(ConnectedAccount, account_id)
    if account is None:
        return
    record_event(
        session,
        account.workspace_id,
        TARGET_PROGRESS,
        "account",
        account_id,
        {"event": event_type, "publish_id": publish_id},
    )
