from __future__ import annotations

import json
from typing import Any

from sqlalchemy import ColumnElement, and_, not_, or_, select
from sqlalchemy.orm import Session

from runway.db.models import AuditEvent, Channel, PublishAttempt
from runway.domain.state_machine import (
    ATTEMPT_REACHED_SUBMISSION_STATUSES,
    ATTEMPT_SETTLED_STATUSES,
)


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


def publish_attempt_may_be_unsettled() -> ColumnElement[bool]:
    """SQL prefilter: every attempt row that is not provably settled.

    A row is excluded only when its status is a settled state AND its
    timestamps match what the lifecycle writes for that state, i.e. exactly
    the rows `attempt_row_status` resolves to a settled state. Everything
    else (unfinished work, unknown strings, impossible timestamp combos) is
    returned so callers can fail closed with `attempt_blocks_new_work`.
    """
    settled = [state.value for state in ATTEMPT_SETTLED_STATUSES]
    settled_after_submission = [
        state.value for state in ATTEMPT_SETTLED_STATUSES & ATTEMPT_REACHED_SUBMISSION_STATUSES
    ]
    settled_before_submission = [
        state.value for state in ATTEMPT_SETTLED_STATUSES - ATTEMPT_REACHED_SUBMISSION_STATUSES
    ]
    provably_settled = and_(
        PublishAttempt.status.in_(settled),
        PublishAttempt.completed_at.is_not(None),
        or_(
            and_(
                PublishAttempt.status.in_(settled_after_submission),
                PublishAttempt.submitted_at.is_not(None),
            ),
            and_(
                PublishAttempt.status.in_(settled_before_submission),
                PublishAttempt.submitted_at.is_(None),
            ),
        ),
    )
    return or_(PublishAttempt.status.is_(None), not_(provably_settled))
