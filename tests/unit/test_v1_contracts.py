"""Shared /api/v1 contracts: problems, router factory, pagination, If-Match."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import Column, Integer, MetaData, String, Table, create_engine, select
from sqlalchemy.orm import Session

from runway.api.v1.concurrency import (
    bump_version,
    etag_for,
    if_match_version,
    parse_if_match,
    require_version,
)
from runway.api.v1.errors import install_problem_handlers
from runway.api.v1.pagination import (
    MAX_LIMIT,
    CursorParams,
    cursor_params,
    decode_cursor,
    encode_cursor,
    paginate,
)
from runway.api.v1.router import V1_PREFIX, V1Route, build_v1_router
from runway.auth.principal import get_principal
from runway.auth.problems import PROBLEM_MEDIA_TYPE, Problem, ProblemCode, default_status
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import EventRecord
from runway.events.log import record_event
from runway.events.types import SYNC_FINISHED

# ---------------------------------------------------------------- problems


def test_every_shared_code_has_its_status() -> None:
    assert {code.value: default_status(code) for code in ProblemCode} == {
        "scope_missing": 403,
        "version_conflict": 409,
        "human_required": 403,
        "capability_not_ready": 409,
        "validation_failed": 422,
        "not_found": 404,
        "idempotency_conflict": 409,
        "rate_limited": 429,
        "internal_error": 500,
        "invalid_state": 409,
        "authentication_required": 401,
        "api_key_unsupported": 401,
    }


def test_problem_body_is_rfc9457_with_a_stable_code() -> None:
    problem = Problem.of(
        ProblemCode.HUMAN_REQUIRED, "TikTok posts need a person", extra={"platform": "tiktok"}
    )
    assert problem.status_code == 403
    assert problem.body() == {
        "type": "https://runway.local/problems/human_required",
        "title": "A person must do this",
        "status": 403,
        "detail": "TikTok posts need a person",
        "code": "human_required",
        "platform": "tiktok",
    }
    # Extra members never override the standard ones.
    assert Problem.of(ProblemCode.NOT_FOUND, "x", extra={"code": "other"}).body()["code"] == (
        "not_found"
    )


class Body(BaseModel):
    name: str


def _app(tmp_path: Path) -> FastAPI:
    application = FastAPI()
    application.state.settings = Settings(data_dir=tmp_path / "data")
    install_problem_handlers(application)
    v1 = build_v1_router(tags=["test"], prefix="/things")

    @v1.post("")
    def create(body: Body) -> dict[str, str]:
        return {"name": body.name}

    @v1.get("/{thing_id}")
    def read(thing_id: int) -> dict[str, int]:
        if thing_id != 1:
            raise Problem.of(ProblemCode.NOT_FOUND, "no such thing")
        return {"id": thing_id}

    @v1.get("")
    def listing(params: CursorParams = Depends(cursor_params)) -> dict[str, object]:
        return {"limit": params.limit, "cursor": params.cursor}

    legacy = APIRouter(prefix="/api", dependencies=[Depends(get_principal)])

    @legacy.post("/legacy")
    def legacy_create(body: Body) -> dict[str, str]:
        return {"name": body.name}

    @legacy.get("/legacy/{item}")
    def legacy_read(item: int) -> dict[str, int]:
        raise HTTPException(status_code=404, detail="item not found")

    application.include_router(v1)
    application.include_router(legacy)
    return application


def test_v1_errors_are_problem_json_and_legacy_errors_are_unchanged(tmp_path: Path) -> None:
    client = TestClient(_app(tmp_path))

    invalid = client.post(f"{V1_PREFIX}/things", json={"name": 3, "secret": "s3cret"})
    assert invalid.status_code == 422
    assert invalid.headers["content-type"].startswith(PROBLEM_MEDIA_TYPE)
    body = invalid.json()
    assert body["code"] == "validation_failed"
    assert body["errors"][0]["loc"] == ["body", "name"]
    assert "s3cret" not in invalid.text and "input" not in body["errors"][0]

    missing = client.get(f"{V1_PREFIX}/things/2")
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"
    unknown = client.get(f"{V1_PREFIX}/nothing-here")
    assert unknown.status_code == 404 and unknown.json()["code"] == "not_found"
    assert client.get(f"{V1_PREFIX}/things?limit=0").json()["code"] == "validation_failed"

    # Unversioned routes keep FastAPI's {"detail": ...} format.
    legacy_invalid = client.post("/api/legacy", json={"name": 3})
    assert legacy_invalid.status_code == 422
    assert legacy_invalid.headers["content-type"] == "application/json"
    assert isinstance(legacy_invalid.json()["detail"], list)
    assert client.get("/api/legacy/1").json() == {"detail": "item not found"}
    assert client.get("/no-such-page").json() == {"detail": "Not Found"}


def test_the_v1_factory_requires_a_principal_and_uses_the_v1_route(tmp_path: Path) -> None:
    router = build_v1_router(tags=["x"], prefix="/x")
    assert router.prefix == "/api/v1/x"
    assert router.route_class is V1Route
    assert [dependency.dependency for dependency in router.dependencies] == [get_principal]
    for bad in ("x", "/x/"):
        with pytest.raises(ValueError):
            build_v1_router(tags=["x"], prefix=bad)
    hosted = _app(tmp_path)
    hosted.state.settings = hosted.state.settings.model_copy(update={"profile": "hosted"})
    response = TestClient(hosted).get(f"{V1_PREFIX}/things/1")
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"


# -------------------------------------------------------------- pagination


def test_cursors_round_trip_and_reject_foreign_or_tampered_values() -> None:
    cursor = encode_cursor(42, scope="events", descending=True)
    assert "42" not in cursor  # opaque
    assert decode_cursor(cursor, scope="events", descending=True) == 42
    for other in (
        lambda: decode_cursor(cursor, scope="posts", descending=True),
        lambda: decode_cursor(cursor, scope="events", descending=False),
        lambda: decode_cursor("not base64 !!", scope="events", descending=True),
        lambda: decode_cursor(
            encode_cursor(1, scope="events", descending=True)[:-3], scope="events", descending=True
        ),
    ):
        with pytest.raises(Problem) as raised:
            other()
        assert raised.value.code == "validation_failed"


@pytest.mark.parametrize("descending", [True, False])
def test_paginate_walks_every_row_once(database: Database, descending: bool) -> None:
    with database.session() as session:
        ids = [record_event(session, 1, SYNC_FINISHED, "account", n, {"n": n}).id for n in range(7)]
    seen: list[int] = []
    cursor: str | None = None
    pages = 0
    with database.session() as session:
        while True:
            rows, cursor = paginate(
                session,
                select(EventRecord).where(EventRecord.workspace_id == 1),
                EventRecord.id,
                CursorParams(cursor=cursor, limit=3),
                scope="events",
                descending=descending,
            )
            seen.extend(row.id for row in rows)
            pages += 1
            if cursor is None:
                break
    assert seen == (sorted(ids, reverse=True) if descending else sorted(ids))
    assert pages == 3


def test_limit_is_bounded() -> None:
    assert cursor_params(cursor="", limit=MAX_LIMIT) == CursorParams(cursor=None, limit=MAX_LIMIT)


# ------------------------------------------------------------- concurrency


@pytest.mark.parametrize(
    ("header", "expected"), [('"3"', 3), ('W/"12"', 12), ("7", 7), (' "0" ', 0)]
)
def test_if_match_accepts_one_version(header: str, expected: int) -> None:
    assert parse_if_match(header) == expected
    assert if_match_version(header) == expected


@pytest.mark.parametrize("header", [None, "", "*", '"1", "2"', '"abc"'])
def test_if_match_must_name_exactly_one_version(header: str | None) -> None:
    with pytest.raises(Problem) as raised:
        parse_if_match(header)
    assert raised.value.status_code == 428
    assert raised.value.code == "validation_failed"


def test_stale_versions_are_a_409_with_the_current_version() -> None:
    require_version(4, 4)
    with pytest.raises(Problem) as raised:
        require_version(5, 4)
    assert raised.value.status_code == 409
    assert raised.value.code == "version_conflict"
    assert raised.value.body()["current_version"] == 5
    assert raised.value.headers == {"ETag": etag_for(5)} == {"ETag": '"5"'}


def test_bump_version_is_a_compare_and_set() -> None:
    metadata = MetaData()
    posts = Table(
        "posts_under_test",
        metadata,
        Column("id", Integer, primary_key=True),
        Column("title", String(20)),
        Column("version", Integer, nullable=False),
    )
    engine = create_engine("sqlite://")
    metadata.create_all(engine)
    with Session(engine) as session:
        session.execute(posts.insert().values(id=1, title="a", version=1))
        assert bump_version(session, posts, 1, 1) == 2
        # A second editor who also read version 1 loses.
        with pytest.raises(Problem) as raised:
            bump_version(session, posts, 1, 1)
        assert raised.value.code == "version_conflict"
        assert raised.value.body()["current_version"] == 2
        with pytest.raises(Problem) as missing:
            bump_version(session, posts, 99, 1)
        assert missing.value.code == "not_found"
    engine.dispose()
