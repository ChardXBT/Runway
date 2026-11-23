"""`set_target_state` is a compare-and-set: a stale session never overwrites.

On Postgres a cancel could read `queued`, a worker could commit `sending`, and
the cancel's unconditional UPDATE would win — the post goes out while Runway
says "cancelled". Every transition the helper writes is now
`UPDATE ... WHERE id=:id AND state=:expected`, so the stale writer gets a 409
conflict and the committed state stands.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from runway.auth.problems import Problem
from runway.db.base import Database
from runway.db.models import ContentDraft, DraftTarget, EventRecord
from runway.domain.enums import TargetState
from runway.posts.states import set_target_state

_T = TargetState


def _queued_target(database: Database) -> tuple[int, int]:
    """A post with one queued target (the state a released target waits in)."""
    with database.session() as session:
        post = ContentDraft(title="Post", body="Hi")
        session.add(post)
        session.flush()
        target = DraftTarget(
            draft_id=post.id,
            platform="youtube",
            post_type="youtube_post",
            state=_T.QUEUED.value,
            status=_T.QUEUED.value,
        )
        session.add(target)
        session.flush()
        return post.id, target.id


def test_a_fresh_session_still_transitions_and_records_its_event(
    database: Database,
) -> None:
    _, target_id = _queued_target(database)
    with database.session() as session:
        target = session.get(DraftTarget, target_id)
        assert target is not None
        set_target_state(session, target, _T.SENDING, reason="worker picked it up")
        # The in-memory object matches what the UPDATE wrote.
        assert (target.state, target.status, target.version) == ("sending", "sending", 2)

    with database.session() as session:
        row = session.get(DraftTarget, target_id)
        assert row is not None
        assert (row.state, row.status, row.state_reason, row.version) == (
            "sending",
            "sending",
            "worker picked it up",
            2,
        )
        changes = [
            event
            for event in session.scalars(
                select(EventRecord).where(EventRecord.type == "target.state_changed")
            )
        ]
        assert len(changes) == 1


def test_a_stale_sessions_cancel_loses_the_race_to_a_committed_sending(
    database: Database,
) -> None:
    _, target_id = _queued_target(database)

    session_a = database.session_factory()
    try:
        target_a = session_a.get(DraftTarget, target_id)
        assert target_a is not None and target_a.state == "queued"

        # Another committed transaction (the send worker) moves the row.
        with database.session() as session_b:
            target_b = session_b.get(DraftTarget, target_id)
            assert target_b is not None
            set_target_state(session_b, target_b, _T.SENDING, reason="send started")

        # Session A still holds `queued`; its cancel must lose, not overwrite.
        with pytest.raises(Problem) as raised:
            set_target_state(session_a, target_a, _T.CANCELLED, reason="cancelled")
        assert raised.value.code == "version_conflict"
        assert raised.value.status_code == 409
        session_a.rollback()
    finally:
        session_a.close()

    with database.session() as session:
        row = session.get(DraftTarget, target_id)
        assert row is not None
        assert (row.state, row.status) == ("sending", "sending")
        # No `cancelled` event was recorded for the lost transition.
        changes = list(session.scalars(select(EventRecord.type)).all())
        assert changes == ["target.state_changed"]
