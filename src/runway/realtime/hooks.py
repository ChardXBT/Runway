"""Publish-progress hooks shared by webhook processors (spec 04 section 7).

A verified `post.publish.*` hint first tries the platform seam: when the
`publish_id` resolves to a known publish target the seam queues an immediate
`poll_processing` job (in its own session, since the webhook handler's
transaction must stay free to commit last). When no target matches, the hint
becomes a `target.progress` event on the account in the caller's transaction.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from runway.db.base import Database
from runway.db.models import ConnectedAccount
from runway.events.log import record_event
from runway.events.types import TARGET_PROGRESS
from runway.platforms import realtime_seam


def on_publish_event(
    session: Session,
    database: Database,
    account_id: int,
    publish_id: str | None,
    event_type: str,
) -> None:
    """Queue an immediate poll for the named target, else record the hint.

    The seam runs first: it writes in its own session, and SQLite admits one
    writer at a time, so it must finish before this session's first flush.
    """
    account = session.get(ConnectedAccount, account_id)
    if account is None:
        return
    if realtime_seam.on_publish_event(
        event_type, database, publish_id=publish_id, account_id=account_id
    ):
        return
    record_event(
        session,
        account.workspace_id,
        TARGET_PROGRESS,
        "account",
        account_id,
        {"event": event_type, "publish_id": publish_id},
    )
