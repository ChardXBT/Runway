"""The one writer of target state, and the one rule for a post's status (spec 03 section 2).

`set_target_state` validates the edge against `TARGET_STATE_MACHINE`, writes
the machine's `state` column and its legacy `status` mirror, bumps the
target's version and records `target.state_changed` (plus
`target.awaiting_release` when an agent-created target becomes ready) in the
caller's transaction, so a committed change always has its event. The write is
a compare-and-set on the state the object was read in: a stale session gets a
409 `version_conflict` instead of silently overwriting a committed move.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from runway.auth.problems import Problem, ProblemCode
from runway.db.models import ContentDraft, DraftTarget
from runway.domain.enums import TargetState
from runway.domain.state_machine import (
    DRAFT_STATUS_MACHINE,
    TARGET_STATE_MACHINE,
    InvalidStateTransition,
    derive_draft_status,
)
from runway.events.log import record_event
from runway.events.types import TARGET_AWAITING_RELEASE, TARGET_STATE_CHANGED
from runway.posts.types import PostSource, PostStatus, target_state_name

logger = logging.getLogger(__name__)

_REASON_LIMIT = 1000

_T = TargetState
_ATTENTION = frozenset({_T.UNCERTAIN, _T.FAILED_RETRYABLE, _T.FAILED_FINAL, _T.NEEDS_USER})
_IN_FLIGHT = frozenset({_T.SENDING, _T.PROCESSING})
_DELIVERED = frozenset({_T.PUBLISHED, _T.VERIFIED})
_WAITING = frozenset({_T.RELEASED, _T.QUEUED})
# The columns set_target_state's compare-and-set UPDATE writes; expired on the
# object afterwards so a stale in-memory value is never reused.
_STATE_COLUMNS = ["state", "status", "state_reason", "version"]


def _state_conflict(session: Session, target: DraftTarget) -> Problem:
    """The compare-and-set matched no row: the target moved (or went) since we read it.

    Same 409 `version_conflict` shape `bump_version` raises for a stale version.
    """
    row = session.execute(
        select(DraftTarget.__table__.c.state, DraftTarget.__table__.c.version).where(
            DraftTarget.__table__.c.id == target.id
        )
    ).one_or_none()
    # The UPDATE did not happen; do not leave the stale read on the object.
    session.expire(target, _STATE_COLUMNS)
    if row is None:
        return Problem.of(ProblemCode.NOT_FOUND, "this item does not exist")
    state, version = row
    return Problem.of(
        ProblemCode.VERSION_CONFLICT,
        "the target changed state; reload and try again",
        extra={"current_state": state, "current_version": int(version)},
        headers={"ETag": f'"{int(version)}"'},
    )


def current_state(target: DraftTarget) -> TargetState:
    """The target's state; refuses a row whose two columns disagree (fail closed)."""
    state = TARGET_STATE_MACHINE.parse(target.state)
    mirror = TARGET_STATE_MACHINE.parse(target.status)
    if mirror is not state:
        raise InvalidStateTransition(TARGET_STATE_MACHINE.name, target.status, target.state)
    return state


def set_target_state(
    session: Session,
    target: DraftTarget,
    new: TargetState,
    *,
    reason: str | None = None,
    post: ContentDraft | None = None,
    event_payload: dict[str, object] | None = None,
) -> TargetState:
    """Move `target` to `new` (validated), with its event, in the caller's transaction.

    The write is a compare-and-set: the UPDATE matches the row only while it is
    still in the state this object was read in, so a session holding a stale
    `queued` can never overwrite a `sending` another transaction committed
    (otherwise a cancelled post would still go out).
    """
    old = current_state(target)
    TARGET_STATE_MACHINE.require(old, new)
    table = DraftTarget.__table__
    result = session.execute(
        update(table)  # type: ignore[arg-type]
        .where(table.c.id == target.id, table.c.state == old.value)
        .values(
            state=new.value,
            status=new.value,
            state_reason=reason[:_REASON_LIMIT] if reason else None,
            version=table.c.version + 1,
        )
        .execution_options(synchronize_session=False)
    )
    if int(getattr(result, "rowcount", 0) or 0) != 1:
        raise _state_conflict(session, target)
    # The row moved; expire the columns the UPDATE wrote so the loaded object
    # says exactly what is committed (the version came from the database).
    session.expire(target, _STATE_COLUMNS)
    owner = post or session.get(ContentDraft, target.draft_id)
    if old is not new:
        payload: dict[str, object] = {
            "post_id": target.draft_id,
            "target_id": target.id,
            "platform": target.platform,
            "post_type": target.post_type,
            "from": target_state_name(old),
            "to": target_state_name(new),
            "reason": target.state_reason,
            "version": target.version,
        }
        payload.update(event_payload or {})
        record_event(
            session, target.workspace_id, TARGET_STATE_CHANGED, "target", target.id, payload
        )
        if new is _T.READY and owner is not None and owner.source == PostSource.AGENT.value:
            record_event(
                session,
                target.workspace_id,
                TARGET_AWAITING_RELEASE,
                "target",
                target.id,
                {
                    "post_id": target.draft_id,
                    "target_id": target.id,
                    "platform": target.platform,
                    "post_type": target.post_type,
                },
            )
    if owner is not None:
        refresh_legacy_status(session, owner)
    return new


def refresh_legacy_status(session: Session, post: ContentDraft) -> None:
    """Keep the unversioned routes' cached draft status in step (never used by v1)."""
    states = list(
        session.scalars(select(DraftTarget.state).where(DraftTarget.draft_id == post.id)).all()
    )
    scheduled = post.scheduled_for is not None or bool(
        session.scalar(
            select(DraftTarget.id)
            .where(DraftTarget.draft_id == post.id, DraftTarget.planned_at.is_not(None))
            .limit(1)
        )
    )
    try:
        derived = derive_draft_status(post.status, scheduled=scheduled, target_states=states)
        DRAFT_STATUS_MACHINE.apply(post, derived)
    except (InvalidStateTransition, ValueError):
        # The cache is advisory; a refused legacy move must not undo the
        # target change it follows.
        logger.debug("legacy draft status of post %s left unchanged", post.id)


def derive_post_status(states: Iterable[TargetState | str]) -> PostStatus:
    """The post's status from its targets. Never stored (spec 03 section 2)."""
    parsed = [TARGET_STATE_MACHINE.parse(value) for value in states]
    live = [state for state in parsed if state is not _T.CANCELLED]
    if not parsed:
        return PostStatus.DRAFT
    if not live:
        return PostStatus.CANCELLED
    if all(state in _DELIVERED for state in live):
        return PostStatus.PUBLISHED
    if any(state in _ATTENTION for state in live):
        return PostStatus.NEEDS_ATTENTION
    if any(state in _IN_FLIGHT for state in live):
        return PostStatus.PUBLISHING
    if any(state in _DELIVERED for state in live):
        return PostStatus.PARTIALLY_PUBLISHED
    if any(state in _WAITING for state in live):
        return PostStatus.SCHEDULED
    if all(state is _T.READY for state in live):
        return PostStatus.READY
    return PostStatus.DRAFT
