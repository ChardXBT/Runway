"""Where verified platform webhooks nudge the publishing engine (spec 08 section 4).

The realtime edge calls `on_publish_event` once a `post.publish.*` webhook is
verified. The hook resolves the publish id (or the target id when the edge
already knows it) to a `draft_targets` row and queues a `poll_processing` job
for immediate pickup, so the engine learns the outcome now instead of on its
next scheduled poll. An event that names no known target is logged and dropped:
a webhook must never create or mutate rows it cannot identify.

No route calls this yet — the edge and its signature checks arrive with the
realtime work. Until then the seam is exercised through tests only.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select

from runway.db.base import Database
from runway.db.models import DraftTarget, utcnow
from runway.posts.jobs import POLL_PROCESSING_JOB, enqueue_in, free_key

logger = logging.getLogger(__name__)


def boost(account_id: int, scope: str, minutes: int) -> None:
    """Ask the realtime sync to hurry for one account; a no-op until it lands.

    Adapters call this after a successful publish so fresh metrics arrive
    sooner. The realtime work connects the real implementation at merge.
    """
    logger.debug(
        "boost requested but the realtime seam is not wired (account_id=%s scope=%s minutes=%s)",
        account_id,
        scope,
        minutes,
    )


def on_publish_event(
    event_type: str,
    database: Database | None = None,
    *,
    publish_id: str | None = None,
    account_id: int | None = None,
    target_id: int | None = None,
    payload: Mapping[str, Any] | None = None,
) -> bool:
    """Queue an immediate status poll for the target a publish event names.

    `publish_id` is the id the video init call returned; it is stored on the
    target as `external_id` when the upload finishes. Returns True when a
    target was found and nudged, False when the event matched nothing.
    """
    if database is None:
        return False
    with database.session() as session:
        target = session.get(DraftTarget, target_id) if target_id is not None else None
        if target is None and publish_id:
            target = session.scalar(
                select(DraftTarget).where(DraftTarget.external_id == publish_id)
            )
        if target is None:
            logger.info(
                "publish event %s matched no target (publish_id=%s account_id=%s)",
                event_type,
                publish_id,
                account_id,
            )
            return False
        enqueue_in(
            session,
            POLL_PROCESSING_JOB,
            idempotency_key=free_key(session, f"poll:{target.id}:webhook:{event_type}"),
            run_at=utcnow(),
            workspace_id=target.workspace_id,
            target_id=target.id,
            payload={"via": "webhook", "event": event_type},
        )
        return True
