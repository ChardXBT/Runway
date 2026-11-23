"""Approval queue, rejection and the release guard (spec 06 sections 4 and 10)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

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
from runway.db.models import (
    AuditLogEntry,
    ConnectedAccount,
    ContentDraft,
    DraftTarget,
    EventRecord,
)

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
    # The real app: /api/v1/posts and its release route come from the publishing engine.
    with TestClient(create_app(settings), base_url=BASE_URL) as test_client:
        yield test_client


@pytest.fixture(autouse=True)
def _default_services() -> Iterator[None]:
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


def _account(database: Database, platform: str) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform=platform,
            external_account_id=f"{platform}-owner",
            handle="@owner",
            status="connected",
            granted_scopes_json="[]",
            token_ref=None,
            metadata_json="{}",
        )
        session.add(account)
        session.flush()
        return account.id


def _create(
    client: TestClient,
    account_id: int,
    *,
    title: str = "Post",
    secret: str | None = None,
    platform: str = "youtube",
    post_type: str | None = "youtube_post",
) -> dict[str, Any]:
    """A post through the real engine, by the owner or (with `secret`) by an API key."""
    target: dict[str, Any] = {"platform": platform, "account_id": account_id}
    if post_type is not None:
        target["post_type"] = post_type
    response = client.post(
        "/api/v1/posts",
        json={"title": title, "body": "Community update", "targets": [target]},
        headers=_bearer(secret) if secret else {},
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _releasable(client: TestClient, post: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Schedule the post's target now (owner) and return its release URL and body."""
    target_id = post["targets"][0]["id"]
    scheduled = client.post(
        f"/api/v1/posts/{post['id']}/targets/{target_id}/schedule", json={"now": True}
    )
    assert scheduled.status_code == 200, scheduled.text
    target = scheduled.json()["targets"][0]
    assert target["releasable"], target
    url = RELEASE_URL.format(post_id=post["id"], target_id=target_id)
    return url, {"payload_hash": target["payload_hash"], "confirmed": True}


def _key(database: Database, settings: Settings, **kwargs: Any) -> str:
    return ApiKeyService(database, settings).create(local_owner(), name="Muse", **kwargs).secret


def _per_key(client: TestClient) -> None:
    assert client.put("/api/v1/policy", json={"agent_release": "per_key"}).status_code == 200


# ------------------------------------------------------------ release guard

_ANY_RELEASE_BODY = {"payload_hash": "0" * 64, "confirmed": True}


def test_a_key_without_posts_release_gets_scope_missing(
    client: TestClient, database: Database, settings: Settings
) -> None:
    youtube = _account(database, "youtube")
    secret = _key(database, settings)
    post = _create(client, youtube, secret=secret)
    url, body = _releasable(client, post)
    response = client.post(url, json=body, headers=_bearer(secret))
    assert response.status_code == 403
    assert response.json()["code"] == "scope_missing"
    target = client.get(f"/api/v1/posts/{post['id']}").json()["targets"][0]
    assert target["state"] == "ready" and target["release"]["state"] == "none"


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
        response = client.post(url, json=_ANY_RELEASE_BODY, headers=_bearer(secret))
        assert response.status_code == 403
        assert response.json()["code"] == "human_required"
    # A person passes the guard; the engine then judges the target itself (a draft).
    by_person = client.post(url, json=_ANY_RELEASE_BODY)
    assert by_person.status_code == 409
    assert by_person.json()["code"] == "invalid_state"


def test_a_granted_key_releases_only_its_platforms_within_its_daily_cap(
    client: TestClient, database: Database, settings: Settings
) -> None:
    youtube = _account(database, "youtube")
    _per_key(client)
    secret = _key(
        database,
        settings,
        scopes=["posts:read", "posts:write", "posts:release"],
        release_grants=[ReleaseGrantInput(platform="youtube", daily_cap=1)],
    )
    first_url, first_body = _releasable(client, _create(client, youtube, secret=secret))
    second_url, second_body = _releasable(client, _create(client, youtube, secret=secret))
    instagram = _target(database, "instagram")
    ok = client.post(first_url, json=first_body, headers=_bearer(secret))
    assert ok.status_code == 200, ok.text
    assert ok.json()["targets"][0]["state"] == "queued"
    capped = client.post(second_url, json=second_body, headers=_bearer(secret))
    assert capped.status_code == 429
    assert capped.json()["code"] == "rate_limited"
    other = client.post(
        RELEASE_URL.format(post_id=instagram[0], target_id=instagram[1]),
        json=_ANY_RELEASE_BODY,
        headers=_bearer(secret),
    )
    assert other.status_code == 403
    assert other.json()["code"] == "scope_missing"
    # Back to never: the stored scope stops working at once.
    assert client.put("/api/v1/policy", json={"agent_release": "never"}).status_code == 200
    again = client.post(second_url, json=second_body, headers=_bearer(secret))
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


def test_the_queue_is_empty_without_agent_posts(client: TestClient) -> None:
    response = client.get("/api/v1/approvals")
    assert response.status_code == 200
    assert response.json() == {"items": [], "next_cursor": None}


def test_the_queue_lists_only_agent_targets_in_ready_from_the_real_engine(
    client: TestClient, database: Database, settings: Settings
) -> None:
    youtube = _account(database, "youtube")
    secret = _key(database, settings)
    agent_one = _create(client, youtube, title="Agent one", secret=secret)
    agent_two = _create(client, youtube, title="Agent two", secret=secret)
    human = _create(client, youtube, title="Human")
    # No post type yet: the agent's target stays in draft, out of the queue.
    agent_draft = _create(client, youtube, title="Agent draft", secret=secret, post_type=None)
    assert human["source"] == "human" and agent_one["source"] == "agent"
    assert agent_one["ai_generated"] is True
    assert agent_draft["targets"][0]["state"] == "draft"

    page = client.get("/api/v1/approvals", params={"limit": 1}).json()
    second = agent_two["targets"][0]
    assert [item["target_id"] for item in page["items"]] == [second["id"]]
    item = page["items"][0]
    assert item["title"] == "Agent two"
    assert item["payload_hash"] == second["payload_hash"]
    assert item["payload"] == second["payload"]
    assert item["ai_generated"] is True and item["source"] == "agent"
    assert page["next_cursor"]
    rest = client.get(
        "/api/v1/approvals", params={"limit": 1, "cursor": page["next_cursor"]}
    ).json()
    assert [item["target_id"] for item in rest["items"]] == [agent_one["targets"][0]["id"]]
    assert rest["next_cursor"] is None
    with database.session() as session:
        waiting = session.scalars(
            select(EventRecord).where(EventRecord.type == "target.awaiting_release")
        ).all()
        assert {event.subject_id for event in waiting} == {
            str(agent_one["targets"][0]["id"]),
            str(second["id"]),
        }


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
    youtube = _account(database, "youtube")
    secret = _key(database, settings)
    created = _create(client, youtube, title="Launch day", secret=secret)
    post_id, target_id = created["id"], created["targets"][0]["id"]
    assert [i["target_id"] for i in client.get("/api/v1/approvals").json()["items"]] == [target_id]
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
    # The engine took the target out of the queue (cancelled: stable, no job, no release).
    assert client.get("/api/v1/approvals").json()["items"] == []
    target = client.get(f"/api/v1/posts/{post_id}").json()["targets"][0]
    assert target["state"] == "cancelled" and target["release"]["state"] == "none"
    # Rejecting it again is refused: it no longer waits for release.
    again = client.post(f"/api/v1/approvals/{target_id}/reject", json=body)
    assert again.status_code == 409 and again.json()["code"] == "invalid_state"

    # A default agent key cannot reject (it needs posts:release); unknown targets are 404.
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
