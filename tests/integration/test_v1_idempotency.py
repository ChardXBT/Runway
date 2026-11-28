"""Idempotency-Key on v1 mutations (spec 06 section 5), against a real temporary database."""

from __future__ import annotations

import threading
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import select

from runway.api.v1.errors import install_problem_handlers
from runway.api.v1.idempotency import (
    IDEMPOTENCY_RETENTION,
    IdempotencyStore,
    Reservation,
    idempotent,
    request_fingerprint,
)
from runway.api.v1.router import V1_PREFIX, build_v1_router
from runway.auth.problems import Problem, ProblemCode
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import IdempotencyKey, utcnow


class Draft(BaseModel):
    title: str


class Calls:
    def __init__(self) -> None:
        self.count = 0
        self.gate: threading.Event | None = None
        self.entered = threading.Event()


def _app(settings: Settings, database: Database, calls: Calls) -> FastAPI:
    application = FastAPI()
    application.state.settings = settings
    application.state.database = database
    install_problem_handlers(application)
    router = build_v1_router(tags=["drafts"], prefix="/drafts")

    @router.post("", status_code=201)
    @idempotent
    def create(draft: Draft) -> dict[str, object]:
        calls.count += 1
        calls.entered.set()
        if calls.gate is not None:
            calls.gate.wait(10)
        if draft.title == "invalid":
            raise Problem.of(ProblemCode.VALIDATION_FAILED, "title is not allowed")
        return {"id": calls.count, "title": draft.title}

    @router.put("/{draft_id}")
    def replace(draft_id: int, draft: Draft) -> dict[str, object]:
        calls.count += 1
        return {"id": draft_id}

    application.include_router(router)
    return application


@pytest.fixture()
def harness(settings: Settings, database: Database) -> tuple[TestClient, Calls]:
    calls = Calls()
    return TestClient(_app(settings, database, calls)), calls


URL = f"{V1_PREFIX}/drafts"


def test_same_key_and_body_replays_the_first_response(harness, database: Database) -> None:
    client, calls = harness
    first = client.post(URL, json={"title": "Hello"}, headers={"Idempotency-Key": "k-1"})
    # Same JSON in a different key order and spacing is the same request.
    again = client.post(
        URL,
        content=b'{ "title" : "Hello" }',
        headers={"Idempotency-Key": "k-1", "Content-Type": "application/json"},
    )

    assert first.status_code == again.status_code == 201
    assert again.json() == first.json() == {"id": 1, "title": "Hello"}
    assert again.headers["idempotent-replayed"] == "true"
    assert "idempotent-replayed" not in first.headers
    assert calls.count == 1
    with database.session() as session:
        row = session.scalar(select(IdempotencyKey))
        assert row is not None
        assert (row.workspace_id, row.principal_id, row.key) == (1, "user:local-owner", "k-1")
        assert row.response_status == 201


def test_same_key_with_a_different_body_is_a_409(harness) -> None:
    client, calls = harness
    client.post(URL, json={"title": "Hello"}, headers={"Idempotency-Key": "k-2"})
    conflict = client.post(URL, json={"title": "Other"}, headers={"Idempotency-Key": "k-2"})

    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"
    assert calls.count == 1


def test_without_a_key_or_on_unmarked_routes_every_request_runs(harness) -> None:
    client, calls = harness
    client.post(URL, json={"title": "a"})
    client.post(URL, json={"title": "a"})
    client.put(f"{URL}/5", json={"title": "a"}, headers={"Idempotency-Key": "ignored"})
    client.put(f"{URL}/5", json={"title": "a"}, headers={"Idempotency-Key": "ignored"})
    assert calls.count == 4


def test_a_failed_request_releases_its_key(harness, database: Database) -> None:
    client, calls = harness
    failed = client.post(URL, json={"title": "invalid"}, headers={"Idempotency-Key": "k-3"})
    assert failed.status_code == 422
    with database.session() as session:
        assert session.scalar(select(IdempotencyKey)) is None
    fixed = client.post(URL, json={"title": "valid"}, headers={"Idempotency-Key": "k-3"})
    assert fixed.status_code == 201
    assert calls.count == 2


def test_a_concurrent_retry_is_refused_while_the_first_runs(harness) -> None:
    client, calls = harness
    calls.gate = threading.Event()
    results: dict[str, int] = {}

    def first() -> None:
        results["first"] = client.post(
            URL, json={"title": "slow"}, headers={"Idempotency-Key": "k-4"}
        ).status_code

    thread = threading.Thread(target=first)
    thread.start()
    assert calls.entered.wait(10)
    retry = client.post(URL, json={"title": "slow"}, headers={"Idempotency-Key": "k-4"})
    calls.gate.set()
    thread.join(10)

    assert retry.status_code == 409
    assert retry.json()["code"] == "idempotency_conflict"
    assert retry.headers["retry-after"] == "1"
    assert results["first"] == 201
    assert calls.count == 1


@pytest.mark.parametrize("key", ["", "has space", "x" * 201])
def test_malformed_keys_are_a_validation_problem(harness, key: str) -> None:
    client, calls = harness
    response = client.post(URL, json={"title": "a"}, headers={"Idempotency-Key": key})
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert calls.count == 0


def test_keys_are_scoped_per_principal_and_expire_after_24_hours(database: Database) -> None:
    now = utcnow()
    clock_value = [now]
    store = IdempotencyStore(database, clock=lambda: clock_value[0])
    fingerprint = request_fingerprint("POST", "/api/v1/x", "", b"{}")

    assert store.reserve(1, "user:a", "k", fingerprint).outcome is Reservation.RESERVED
    store.complete(1, "user:a", "k", 200, '{"ok":true}')
    assert store.reserve(1, "user:b", "k", fingerprint).outcome is Reservation.RESERVED
    replay = store.reserve(1, "user:a", "k", fingerprint)
    assert (replay.outcome, replay.status, replay.body) == (Reservation.REPLAY, 200, '{"ok":true}')

    clock_value[0] = now + IDEMPOTENCY_RETENTION + timedelta(seconds=1)
    assert store.reserve(1, "user:a", "k", fingerprint).outcome is Reservation.RESERVED
    with database.session() as session:
        # user:b's reservation is older than 24 h now; user:a's was just renewed.
        assert store.purge_expired(session) == 1
    with database.session() as session:
        assert [row.principal_id for row in session.scalars(select(IdempotencyKey))] == ["user:a"]


def test_fingerprint_covers_method_path_query_and_body() -> None:
    base = request_fingerprint("POST", "/a", "", b'{"x":1}')
    assert base == request_fingerprint("post", "/a", "", b'{ "x": 1 }')
    assert base != request_fingerprint("PUT", "/a", "", b'{"x":1}')
    assert base != request_fingerprint("POST", "/b", "", b'{"x":1}')
    assert base != request_fingerprint("POST", "/a", "q=1", b'{"x":1}')
    assert base != request_fingerprint("POST", "/a", "", b'{"x":2}')
