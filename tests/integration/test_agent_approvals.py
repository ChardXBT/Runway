"""Approval queue, rejection and the release guard (spec 06 sections 4 and 10)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text

from runway.agent.services import (
    AgentServices,
    ApprovalItem,
    ApprovalPage,
    UnconnectedMediaService,
    UnconnectedPostService,
    register_agent_services,
)
from runway.api.app import create_app
from runway.api.v1.errors import install_problem_handlers
from runway.api.v1.router import build_v1_router
from runway.auth.api_keys import ApiKeyService
from runway.auth.audit import AgentAuditMiddleware
from runway.auth.principal import Principal, get_principal, local_owner
from runway.auth.release_policy import ReleaseGrantInput
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import AuditLogEntry, ContentDraft, DraftTarget, EventRecord

BASE_URL = "http://127.0.0.1:8000"
RELEASE_URL = "/api/v1/posts/{post_id}/targets/{target_id}/release"


def _release_router() -> Any:
    """Stands in for the engine's release route; the request policy guards it."""
    router = build_v1_router(tags=["test-release"])

    @router.post("/posts/{post_id}/targets/{target_id}/release")
    def release(
        post_id: int, target_id: int, principal: Principal = Depends(get_principal)
    ) -> dict[str, object]:
        return {"released": target_id, "by": principal.kind}

    return router


@pytest.fixture()
def client(settings: Settings, database: Database) -> Iterator[TestClient]:
    application = create_app(settings)
    application.include_router(_release_router())
    with TestClient(application, base_url=BASE_URL) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _unconnected_engine() -> Iterator[None]:
    register_agent_services(None)
    yield
    register_agent_services(None)


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _target(database: Database, platform: str, title: str = "Post") -> tuple[int, int]:
    with database.session() as session:
        draft = ContentDraft(title=title, body="")
        session.add(draft)
        session.flush()
        target = DraftTarget(draft_id=draft.id, platform=platform)
        session.add(target)
        session.flush()
        return draft.id, target.id


def _key(database: Database, settings: Settings, **kwargs: Any) -> str:
    return ApiKeyService(database, settings).create(local_owner(), name="Muse", **kwargs).secret


def _per_key(client: TestClient) -> None:
    assert client.put("/api/v1/policy", json={"agent_release": "per_key"}).status_code == 200


# ------------------------------------------------------------ release guard


def test_a_key_without_posts_release_gets_scope_missing(
    client: TestClient, database: Database, settings: Settings
) -> None:
    post_id, target_id = _target(database, "youtube")
    secret = _key(database, settings)
    response = client.post(
        RELEASE_URL.format(post_id=post_id, target_id=target_id), headers=_bearer(secret)
    )
    assert response.status_code == 403
    assert response.json()["code"] == "scope_missing"


def test_tiktok_release_by_any_key_is_human_required(
    client: TestClient, database: Database, settings: Settings
) -> None:
    post_id, target_id = _target(database, "tiktok")
    url = RELEASE_URL.format(post_id=post_id, target_id=target_id)
    plain = _key(database, settings)
    _per_key(client)
    releaser = _key(
        database,
        settings,
        scopes=["posts:read", "posts:release"],
        release_grants=[ReleaseGrantInput(platform="youtube")],
    )
    for secret in (plain, releaser):
        response = client.post(url, headers=_bearer(secret))
        assert response.status_code == 403
        assert response.json()["code"] == "human_required"
    # A person may release it.
    assert client.post(url).json() == {"released": target_id, "by": "user"}


def test_a_granted_key_releases_only_its_platforms_within_its_daily_cap(
    client: TestClient, database: Database, settings: Settings
) -> None:
    _per_key(client)
    secret = _key(
        database,
        settings,
        scopes=["posts:read", "posts:release"],
        release_grants=[ReleaseGrantInput(platform="youtube", daily_cap=1)],
    )
    first = _target(database, "youtube")
    second = _target(database, "youtube")
    instagram = _target(database, "instagram")
    ok = client.post(
        RELEASE_URL.format(post_id=first[0], target_id=first[1]), headers=_bearer(secret)
    )
    assert ok.status_code == 200
    capped = client.post(
        RELEASE_URL.format(post_id=second[0], target_id=second[1]), headers=_bearer(secret)
    )
    assert capped.status_code == 429
    assert capped.json()["code"] == "rate_limited"
    other = client.post(
        RELEASE_URL.format(post_id=instagram[0], target_id=instagram[1]), headers=_bearer(secret)
    )
    assert other.status_code == 403
    assert other.json()["code"] == "scope_missing"
    # Back to never: the stored scope stops working at once.
    assert client.put("/api/v1/policy", json={"agent_release": "never"}).status_code == 200
    again = client.post(
        RELEASE_URL.format(post_id=second[0], target_id=second[1]), headers=_bearer(secret)
    )
    assert again.status_code == 403
    assert again.json()["code"] == "scope_missing"


def test_scope_missing_on_release_in_the_hosted_profile_too(
    settings: Settings, database: Database
) -> None:
    # Hosted Settings cannot be validated yet (SQLite only); model_copy skips that.
    hosted = settings.model_copy(update={"profile": "hosted"})
    application = FastAPI()
    application.state.settings = hosted
    application.state.database = database
    install_problem_handlers(application)
    application.add_middleware(AgentAuditMiddleware, database=database)
    application.include_router(_release_router())
    post_id, target_id = _target(database, "youtube")
    secret = ApiKeyService(database, hosted).create(local_owner(), name="Muse").secret
    assert secret.startswith("rwy_live_")
    client = TestClient(application)
    url = RELEASE_URL.format(post_id=post_id, target_id=target_id)
    response = client.post(url, headers=_bearer(secret))
    assert response.status_code == 403
    assert response.json()["code"] == "scope_missing"
    # No key in hosted is 401, never the owner.
    assert client.post(url).status_code == 401


# ---------------------------------------------------------- approvals queue


def test_the_queue_is_empty_while_the_engine_columns_are_absent(client: TestClient) -> None:
    response = client.get("/api/v1/approvals")
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def _add_engine_columns(database: Database) -> None:
    """The columns 0017_publishing_engine adds, so the guarded read can be exercised."""
    statements = [
        "ALTER TABLE draft_targets ADD COLUMN state VARCHAR(30) NOT NULL DEFAULT 'pending'",
        "ALTER TABLE draft_targets ADD COLUMN post_type VARCHAR(40)",
        "ALTER TABLE draft_targets ADD COLUMN payload_hash VARCHAR(64)",
        "ALTER TABLE draft_targets ADD COLUMN version INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE content_drafts ADD COLUMN source VARCHAR(20) NOT NULL DEFAULT 'human'",
        "ALTER TABLE content_drafts ADD COLUMN ai_generated BOOLEAN NOT NULL DEFAULT 0",
        "ALTER TABLE content_drafts ADD COLUMN created_by VARCHAR(200)",
    ]
    with database.engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def _mark(database: Database, target_id: int, *, state: str, source: str) -> None:
    with database.engine.begin() as connection:
        connection.execute(
            text("UPDATE draft_targets SET state = :state, payload_hash = 'abc' WHERE id = :id"),
            {"state": state, "id": target_id},
        )
        connection.execute(
            text(
                "UPDATE content_drafts SET source = :source, ai_generated = 1,"
                " created_by = 'agent:1'"
                " WHERE id = (SELECT draft_id FROM draft_targets WHERE id = :id)"
            ),
            {"source": source, "id": target_id},
        )


def test_the_guarded_read_lists_only_agent_targets_in_ready(
    client: TestClient, database: Database
) -> None:
    _add_engine_columns(database)
    agent_one = _target(database, "youtube", "Agent one")
    agent_two = _target(database, "instagram", "Agent two")
    human_ready = _target(database, "youtube", "Human")
    agent_draft = _target(database, "youtube", "Agent draft")
    _mark(database, agent_one[1], state="ready", source="agent")
    _mark(database, agent_two[1], state="ready", source="agent")
    _mark(database, human_ready[1], state="ready", source="human")
    _mark(database, agent_draft[1], state="pending", source="agent")

    page = client.get("/api/v1/approvals", params={"limit": 1}).json()
    assert [item["target_id"] for item in page["items"]] == [agent_two[1]]
    assert page["items"][0]["title"] == "Agent two"
    assert page["items"][0]["payload_hash"] == "abc"
    assert page["items"][0]["ai_generated"] is True
    assert page["next_cursor"]
    rest = client.get(
        "/api/v1/approvals", params={"limit": 1, "cursor": page["next_cursor"]}
    ).json()
    assert [item["target_id"] for item in rest["items"]] == [agent_one[1]]
    assert rest["next_cursor"] is None


class _FakePosts(UnconnectedPostService):
    def __init__(self) -> None:
        self.rejected: list[tuple[int, str]] = []

    def list_awaiting_release(
        self, workspace_id: int, cursor: str | None, limit: int
    ) -> ApprovalPage:
        return ApprovalPage(
            items=[
                ApprovalItem(target_id=7, post_id=3, platform="youtube", account_id=1, title="T")
            ],
            next_cursor=None,
        )

    def reject_target(self, principal: Principal, target_id: int, reason: str) -> None:
        self.rejected.append((target_id, reason))


def test_a_connected_engine_answers_the_queue_and_the_reject_hook(
    client: TestClient, database: Database
) -> None:
    posts = _FakePosts()
    register_agent_services(
        lambda _db, _settings: AgentServices(
            posts=posts, media=UnconnectedMediaService(), connected=True
        )
    )
    assert [item["target_id"] for item in client.get("/api/v1/approvals").json()["items"]] == [7]
    _post_id, target_id = _target(database, "youtube")
    response = client.post(f"/api/v1/approvals/{target_id}/reject", json={"reason": "Off brand"})
    assert response.status_code == 200
    assert posts.rejected == [(target_id, "Off brand")]


# ---------------------------------------------------------------- rejection


def test_rejecting_records_the_reason_and_edits_as_structured_data(
    client: TestClient, database: Database, settings: Settings
) -> None:
    post_id, target_id = _target(database, "youtube", "Launch day")
    body = {
        "reason": "Wrong tone for the audience",
        "edits": [{"field": "title", "before": "Launch day", "after": "We launched"}],
    }
    response = client.post(f"/api/v1/approvals/{target_id}/reject", json=body)
    assert response.status_code == 200
    payload = response.json()
    assert payload["rejected"] is True
    assert payload["reason"] == body["reason"]
    with database.session() as session:
        event = session.scalars(
            select(EventRecord).where(EventRecord.type == "target.rejected")
        ).one()
        data = json.loads(event.payload_json)
        assert event.subject_id == str(target_id)
        assert data["reason"] == body["reason"]
        assert data["post_id"] == post_id
        assert data["edits"] == [{"field": "title", "before": "Launch day", "after": "We launched"}]
        audit = session.scalars(
            select(AuditLogEntry).where(AuditLogEntry.action == "target.reject")
        ).one()
        assert audit.target_id == str(target_id)

    # A default agent key cannot reject (it needs posts:release); unknown targets are 404.
    secret = _key(database, settings)
    refused = client.post(
        f"/api/v1/approvals/{target_id}/reject", json=body, headers=_bearer(secret)
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "scope_missing"
    missing = client.post("/api/v1/approvals/999999/reject", json=body)
    assert missing.status_code == 404
    assert missing.json()["code"] == "not_found"
    assert (
        client.post(f"/api/v1/approvals/{target_id}/reject", json={"reason": ""}).status_code == 422
    )


def test_listing_needs_posts_read(
    client: TestClient, database: Database, settings: Settings
) -> None:
    secret = _key(database, settings, scopes=["content:read"])
    response = client.get("/api/v1/approvals", headers=_bearer(secret))
    assert response.status_code == 403
    assert response.json()["code"] == "scope_missing"
