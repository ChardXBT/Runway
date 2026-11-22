"""GET /api/v1/events/stream over real HTTP: disconnect, resume, every event once, in order.

The app runs in uvicorn on a random free loopback port (never 8000/3000)
against a temporary migrated database.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
import uvicorn
from sqlalchemy import text, update

from runway.api.app import create_app
from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import EventRecord, utcnow
from runway.events import types
from runway.events.log import EVENT_RETENTION, record_event


@dataclass(frozen=True)
class Frame:
    id: str | None
    event: str | None
    data: dict[str, object]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def server(tmp_path: Path) -> Iterator[tuple[str, Database]]:
    settings = Settings(data_dir=tmp_path / "data")
    initialize_database(settings).engine.dispose()
    application = create_app(settings)
    port = _free_port()
    config = uvicorn.Config(
        application,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        timeout_graceful_shutdown=2,
    )
    instance = uvicorn.Server(config)
    thread = threading.Thread(target=instance.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 30
    while not instance.started:
        assert thread.is_alive(), "uvicorn exited during startup"
        assert time.monotonic() < deadline, "uvicorn did not start"
        time.sleep(0.05)
    database: Database = application.state.database
    with database.session() as session:
        session.execute(
            text(
                "INSERT INTO workspaces (id, slug, name, created_at) "
                "VALUES (2, 'other', 'Other', CURRENT_TIMESTAMP)"
            )
        )
    try:
        yield f"http://127.0.0.1:{port}/api/v1/events/stream", database
    finally:
        instance.should_exit = True
        thread.join(timeout=30)


def _record(database: Database, workspace_id: int, subject_id: int) -> int:
    with database.session() as session:
        row = record_event(
            session, workspace_id, types.COMMENT_NEW, "comment", subject_id, {"n": subject_id}
        )
        return row.id


def _read_frames(
    client: httpx.Client, url: str, headers: dict[str, str], wanted: int, on_open=None
) -> list[Frame]:  # type: ignore[no-untyped-def]
    """Read `wanted` event frames, then disconnect by closing the response."""
    frames: list[Frame] = []
    with client.stream("GET", url, headers=headers) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        fields: dict[str, str] = {}
        for line in response.iter_lines():
            if line.startswith("retry:"):
                # Sent once the stream has fixed its start position; events
                # written from here on must all be delivered.
                if on_open is not None:
                    on_open()
                continue
            if line == "":
                if "event" in fields:
                    frames.append(
                        Frame(fields.get("id"), fields["event"], json.loads(fields["data"]))
                    )
                fields = {}
                if len(frames) >= wanted:
                    break
                continue
            if line.startswith(":"):
                continue
            name, _, value = line.partition(":")
            fields[name] = value.removeprefix(" ")
    return frames


def test_a_client_that_disconnects_and_resumes_sees_every_event_once_in_order(
    server: tuple[str, Database],
) -> None:
    url, database = server
    recorded: list[int] = []

    def produce_first_batch() -> None:
        for n in (1, 2, 3):
            recorded.append(_record(database, 1, n))
        _record(database, 2, 999)  # another workspace: never delivered

    with httpx.Client(timeout=15) as client:
        first = _read_frames(client, url, {}, 3, on_open=produce_first_batch)
        last_seen = first[-1].id
        assert last_seen is not None

        # Written while the client is away.
        for n in (4, 5):
            recorded.append(_record(database, 1, n))
        _record(database, 2, 998)

        second = _read_frames(client, url, {"Last-Event-ID": last_seen}, 2)

    delivered = first + second
    assert [int(frame.id or 0) for frame in delivered] == recorded
    assert [frame.data["subject_id"] for frame in delivered] == ["1", "2", "3", "4", "5"]
    assert {frame.event for frame in delivered} == {"comment.new"}


def test_resuming_from_an_id_older_than_retention_starts_with_resync(
    server: tuple[str, Database],
) -> None:
    url, database = server
    old = _record(database, 1, 1)
    with database.session() as session:
        session.execute(
            update(EventRecord)
            .where(EventRecord.id == old)
            .values(created_at=utcnow() - EVENT_RETENTION - timedelta(hours=1))
        )

    with httpx.Client(timeout=15) as client:
        frames = _read_frames(
            client,
            url,
            {"Last-Event-ID": str(old)},
            2,
            on_open=lambda: _record(database, 1, 2),
        )

    assert frames[0].event == "resync"
    assert frames[0].data == {"reason": "last_event_id_unavailable", "resume_from": old}
    assert frames[1].event == "comment.new" and frames[1].data["subject_id"] == "2"


def test_a_malformed_last_event_id_is_a_validation_problem(server: tuple[str, Database]) -> None:
    url, _database = server
    response = httpx.get(url, headers={"Last-Event-ID": "abc"}, timeout=15)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
