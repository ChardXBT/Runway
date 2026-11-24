"""The events table, record_event, resume rules and the SSE frame loop."""

from __future__ import annotations

import json
from datetime import timedelta

import anyio
import pytest
from sqlalchemy import select, text, update

from runway.db.base import Database
from runway.db.models import EventRecord, utcnow
from runway.events import types
from runway.events.log import (
    EVENT_RETENTION,
    EventPayloadTooLarge,
    UnknownEventType,
    events_after,
    purge_expired_events,
    record_event,
    resume_is_complete,
)
from runway.events.stream import HEARTBEAT, event_stream, format_event, stream_start


def _second_workspace(database: Database) -> None:
    with database.session() as session:
        session.execute(
            text(
                "INSERT INTO workspaces (id, slug, name, created_at) "
                "VALUES (2, 'second', 'Second', CURRENT_TIMESTAMP)"
            )
        )


def test_event_types_are_declared_once() -> None:
    declared = {
        value
        for name, value in vars(types).items()
        if name.isupper() and isinstance(value, str) and name != "RESYNC"
    }
    assert declared == types.EVENT_TYPES
    assert types.TARGET_STATE_CHANGED == "target.state_changed"
    assert types.RESYNC not in types.EVENT_TYPES


def test_record_event_writes_in_the_callers_transaction(database: Database) -> None:
    with pytest.raises(RuntimeError), database.session() as session:
        record_event(session, 1, types.SYNC_STARTED, "account", 7, {"run_id": 3})
        raise RuntimeError("the change failed")
    with database.session() as session:
        assert session.scalars(select(EventRecord)).all() == []

    with database.session() as session:
        row = record_event(session, 1, types.SYNC_STARTED, "account", 7, {"run_id": 3})
        assert row.id is not None  # flushed, visible to the same transaction
    with database.session() as session:
        stored = session.scalars(select(EventRecord)).one()
        assert (stored.type, stored.subject_type, stored.subject_id) == (
            "sync.started",
            "account",
            "7",
        )
        assert json.loads(stored.payload_json) == {"run_id": 3}


def test_record_event_refuses_unknown_types_and_large_payloads(database: Database) -> None:
    with database.session() as session:
        with pytest.raises(UnknownEventType):
            record_event(session, 1, "post.exploded", "post", 1)
        with pytest.raises(EventPayloadTooLarge):
            record_event(session, 1, types.COMMENT_NEW, "comment", 1, {"text": "x" * 5000})


def test_event_ids_never_repeat_after_a_purge(database: Database) -> None:
    with database.session() as session:
        first = record_event(session, 1, types.QUOTA_LOW, "workspace", 1).id
    with database.session() as session:
        session.execute(
            update(EventRecord).values(created_at=utcnow() - EVENT_RETENTION - timedelta(hours=1))
        )
        assert purge_expired_events(session) == 1
    with database.session() as session:
        assert record_event(session, 1, types.QUOTA_LOW, "workspace", 1).id > first


def test_resume_rules(database: Database) -> None:
    _second_workspace(database)
    with database.session() as session:
        mine = record_event(session, 1, types.MEDIA_READY, "media", 1).id
    # A workspace-2 row cannot be written through a workspace-1 session; the
    # seed goes through a system session like real cross-workspace work.
    with database.system_session() as session:
        theirs = record_event(session, 2, types.MEDIA_READY, "media", 2).id
    with database.session() as session:
        old = record_event(session, 1, types.MEDIA_READY, "media", 3).id
        session.execute(
            update(EventRecord)
            .where(EventRecord.id == old)
            .values(created_at=utcnow() - EVENT_RETENTION - timedelta(minutes=1))
        )
    with database.session() as session:
        assert resume_is_complete(session, 1, mine)
        assert not resume_is_complete(session, 1, theirs)  # another workspace's id
        assert not resume_is_complete(session, 1, old)  # older than 7 days
        assert not resume_is_complete(session, 1, old + 100)  # from a different database
        assert not resume_is_complete(session, 1, 0)
        assert stream_start(session, 1, None).after_id == old
        assert stream_start(session, 1, mine).resync_reason is None
        resync = stream_start(session, 1, theirs)
        assert resync.resync_reason == "last_event_id_unavailable"
        assert resync.after_id == old
        assert [event.id for event in events_after(session, 1, 0, 10)] == [mine, old]


def test_frames_carry_the_row_id_type_and_a_small_summary(database: Database) -> None:
    with database.session() as session:
        row = record_event(session, 1, types.TARGET_STATE_CHANGED, "target", 5, {"to": "queued"})
    with database.session() as session:
        (event,) = events_after(session, 1, 0, 1)
    frame = format_event(event)
    lines = frame.split("\n")
    assert lines[0] == f"id: {row.id}"
    assert lines[1] == "event: target.state_changed"
    data = json.loads(lines[2].removeprefix("data: "))
    assert data["subject_id"] == "5" and data["payload"] == {"to": "queued"}
    assert frame.endswith("\n\n")


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


class StepWakeup:
    """Advances the fake clock instead of sleeping."""

    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.waits: list[float] = []

    async def wait(self, timeout: float) -> None:
        self.waits.append(timeout)
        self.clock.now += timeout
        await anyio.lowlevel.checkpoint()


def _collect(database: Database, frames_wanted: int, **kwargs) -> list[str]:  # type: ignore[no-untyped-def]
    clock = FakeClock()
    wakeup = StepWakeup(clock)
    frames: list[str] = []

    async def disconnected() -> bool:
        return len(frames) >= frames_wanted

    async def run() -> None:
        async for frame in event_stream(
            database,
            1,
            kwargs.pop("last_event_id", None),
            wakeup=wakeup,
            is_disconnected=disconnected,
            clock=clock,
            **kwargs,
        ):
            frames.append(frame)

    anyio.run(run)
    return frames


def test_an_idle_stream_sends_a_heartbeat_every_20_seconds(database: Database) -> None:
    frames = _collect(database, 3, heartbeat_seconds=20.0)
    assert frames[0] == "retry: 3000\n\n"
    assert frames[1:] == [HEARTBEAT, HEARTBEAT]


def test_resume_replays_only_this_workspaces_later_events_in_order(database: Database) -> None:
    _second_workspace(database)
    with database.system_session() as session:
        record_event(session, 2, types.COMMENT_NEW, "comment", 99)
    with database.session() as session:
        first = record_event(session, 1, types.COMMENT_NEW, "comment", 1).id
        second = record_event(session, 1, types.COMMENT_NEW, "comment", 2).id
        third = record_event(session, 1, types.COMMENT_NEW, "comment", 3).id
    frames = _collect(database, 3, last_event_id=first)
    assert [frame.split("\n")[0] for frame in frames[1:]] == [f"id: {second}", f"id: {third}"]


def test_an_unavailable_last_event_id_starts_with_resync(database: Database) -> None:
    frames = _collect(database, 2, last_event_id=12345)
    assert frames[1].startswith("id: 0\nevent: resync\n")
    assert json.loads(frames[1].split("\n")[2].removeprefix("data: "))["reason"] == (
        "last_event_id_unavailable"
    )
