"""Server-sent event framing and the resumable stream loop (spec 04 section 4).

The `events` table is the truth. A wake-up source only says "look again":
SQLite polls every second; Postgres will use LISTEN/NOTIFY (hook below).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import anyio
from sqlalchemy.orm import Session

from runway.db.base import Database
from runway.events.log import Event, events_after, latest_event_id, resume_is_complete
from runway.events.types import RESYNC

POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 20.0
BATCH_LIMIT = 500
# How long a browser waits before reconnecting after the stream drops.
RETRY_MILLISECONDS = 3000


def format_event(event: Event) -> str:
    data = json.dumps(event.as_dict(), separators=(",", ":"))
    return f"id: {event.id}\nevent: {event.type}\ndata: {data}\n\n"


def format_resync(resume_from: int, reason: str) -> str:
    data = json.dumps({"reason": reason, "resume_from": resume_from}, separators=(",", ":"))
    return f"id: {resume_from}\nevent: {RESYNC}\ndata: {data}\n\n"


HEARTBEAT = ": heartbeat\n\n"


class Wakeup(Protocol):
    async def wait(self, timeout: float) -> None:
        """Return when new events may exist, or after `timeout` seconds."""


class PollingWakeup:
    """SQLite: nothing can notify us, so look again after the poll interval."""

    def __init__(self, interval: float = POLL_SECONDS) -> None:
        self.interval = interval

    async def wait(self, timeout: float) -> None:
        await anyio.sleep(min(self.interval, timeout))


class PostgresNotifyWakeup:
    """POSTGRES HOOK (phase P7): wake on `LISTEN runway_events`.

    The writer issues `NOTIFY runway_events, '<workspace_id>'` in the same
    transaction as `record_event`; this waits on the listening connection
    with `timeout` as an upper bound. Not implemented while Runway runs on
    SQLite only; `wakeup_for` never returns it yet.
    """

    async def wait(self, timeout: float) -> None:
        raise NotImplementedError("LISTEN/NOTIFY wake-up arrives with Postgres support (P7)")


def wakeup_for(database: Database, *, poll_seconds: float = POLL_SECONDS) -> Wakeup:
    # Postgres would return PostgresNotifyWakeup() here once implemented.
    return PollingWakeup(poll_seconds)


@dataclass(frozen=True, slots=True)
class StreamStart:
    after_id: int
    resync_reason: str | None


def stream_start(session: Session, workspace_id: int, last_event_id: int | None) -> StreamStart:
    """Where a (re)connecting client continues from.

    Without Last-Event-ID the client has just loaded current state, so only
    events from now on are sent.
    """
    newest = latest_event_id(session)
    if last_event_id is None:
        return StreamStart(after_id=newest, resync_reason=None)
    if resume_is_complete(session, workspace_id, last_event_id):
        return StreamStart(after_id=last_event_id, resync_reason=None)
    return StreamStart(after_id=newest, resync_reason="last_event_id_unavailable")


async def event_stream(
    database: Database,
    workspace_id: int,
    last_event_id: int | None,
    *,
    wakeup: Wakeup,
    is_disconnected: Callable[[], Awaitable[bool]],
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    visible: Callable[[str], bool] | None = None,
) -> AsyncIterator[str]:
    """Yield SSE frames for one workspace until the client disconnects.

    `visible(type)` filters events by type (an API key sees only what its
    scopes may read); a filtered event still advances the position.
    """

    def start() -> StreamStart:
        with database.session() as session:
            return stream_start(session, workspace_id, last_event_id)

    def fetch(after: int) -> list[Event]:
        with database.session() as session:
            return events_after(session, workspace_id, after, BATCH_LIMIT)

    position = await anyio.to_thread.run_sync(start)
    cursor = position.after_id
    # The first frame is written only after the start position is fixed, so a
    # client that loads current state after receiving it misses nothing.
    yield f"retry: {RETRY_MILLISECONDS}\n\n"
    if position.resync_reason is not None:
        yield format_resync(cursor, position.resync_reason)
    last_write = clock()
    while not await is_disconnected():
        batch = await anyio.to_thread.run_sync(fetch, cursor)
        for event in batch:
            cursor = event.id
            if visible is None or visible(event.type):
                yield format_event(event)
        if batch:
            last_write = clock()
            if len(batch) == BATCH_LIMIT:
                continue
        elif clock() - last_write >= heartbeat_seconds:
            yield HEARTBEAT
            last_write = clock()
        remaining = max(heartbeat_seconds - (clock() - last_write), 0.0)
        await wakeup.wait(remaining)
