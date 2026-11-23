"""`/api/v1/posts` over HTTP: contracts, release rules and an assisted post end to end."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from runway.api.app import create_app
from runway.auth.principal import ALL_SCOPES, Principal, get_principal
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    ConnectedAccount,
    ContentDraft,
    EventRecord,
    PlatformContent,
    PublishJob,
)
from runway.jobs.queue import JobQueue
from runway.jobs.worker import Worker
from runway.posts.engine import build_publishing_engine

BASE = "http://127.0.0.1:8000"


def _account(database: Database, platform: str) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform=platform,
            external_account_id=f"{platform}-owner",
            handle="@owner",
            status="connected",
            granted_scopes_json="[]",
            token_ref=None,  # no credentials at all
            metadata_json="{}",
        )
        session.add(account)
        session.flush()
        return account.id


@pytest.fixture()
def client(database: Database, settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url=BASE) as web:
        yield web


def _run_worker(database: Database, settings: Settings) -> None:
    """Run every job that is due, pulling later jobs forward, as a worker would over time."""
    engine = build_publishing_engine(settings, database)
    queue = JobQueue(database, expired_send_handler=engine.handle_expired_send)
    for _ in range(30):
        with database.session() as session:
            session.execute(
                update(PublishJob)
                .where(PublishJob.state == "pending")
                .values(run_at=datetime.now(UTC) - timedelta(seconds=1))
            )
        if not Worker(queue=queue, handlers=engine.handlers(), worker_id="t").run_once():
            return


def _create(client: TestClient, account_id: int, **target: object) -> dict[str, object]:
    response = client.post(
        "/api/v1/posts",
        json={
            "title": "Hello",
            "body": "Community update",
            "targets": [
                {"platform": "youtube", "account_id": account_id, "post_type": "youtube_post"}
                | target
            ],
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    assert response.headers["etag"] == f'"{body["version"]}"'
    return body


def test_assisted_adapter_completes_a_post_end_to_end_without_credentials(
    client: TestClient, database: Database, settings: Settings
) -> None:
    account = _account(database, "youtube")
    post = _create(client, account)
    target = post["targets"][0]  # type: ignore[index]
    assert target["state"] == "ready" and target["publisher"] == "assisted"
    assert target["capability"]["state"] == "assisted_only"
    assert not target["releasable"]  # no planned time yet

    scheduled = client.post(
        f"/api/v1/posts/{post['id']}/targets/{target['id']}/schedule", json={"now": True}
    )
    assert scheduled.status_code == 200, scheduled.text
    target = scheduled.json()["targets"][0]
    assert target["releasable"]
    released = client.post(
        f"/api/v1/posts/{post['id']}/targets/{target['id']}/release",
        json={"payload_hash": target["payload_hash"], "confirmed": True},
    )
    assert released.status_code == 200, released.text
    assert released.json()["targets"][0]["state"] == "queued"

    _run_worker(database, settings)
    waiting = client.get(f"/api/v1/posts/{post['id']}").json()["targets"][0]
    assert waiting["state"] == "needs_user"
    checklist = waiting["last_attempt"]["response"]["checklist"]
    assert any("mark it as published" in step for step in checklist)

    url = "https://www.youtube.com/post/UgkxABCDEF"
    marked = client.post(
        f"/api/v1/posts/{post['id']}/targets/{target['id']}/mark-published",
        json={"published_url": url},
    )
    assert marked.status_code == 200, marked.text
    assert marked.json()["targets"][0]["state"] == "published"
    # Sync later imports the post from the platform; verify re-reads it from there.
    with database.session() as session:
        session.add(
            PlatformContent(
                account_id=account,
                platform="youtube",
                external_id="UgkxABCDEF",
                content_type="post",
                permalink=url,
            )
        )
    _run_worker(database, settings)

    final = client.get(f"/api/v1/posts/{post['id']}").json()
    assert final["targets"][0]["state"] == "verified"
    assert final["status"] == "published"
    attempts = client.get(f"/api/v1/posts/{post['id']}/targets/{target['id']}/attempts").json()
    assert [item["outcome"] for item in attempts["items"]] == [
        "verified",
        "published",
        "needs_user",
    ]
    with database.session() as session:
        changes = session.scalars(
            select(EventRecord.type).where(EventRecord.subject_id == str(target["id"]))
        ).all()
    assert changes.count("target.state_changed") >= 6


def test_idempotent_create_replays_one_post(client: TestClient, database: Database) -> None:
    account = _account(database, "youtube")
    body = {
        "title": "Once",
        "targets": [{"platform": "youtube", "account_id": account, "post_type": "youtube_post"}],
    }
    first = client.post("/api/v1/posts", json=body, headers={"Idempotency-Key": "abc-1"})
    second = client.post("/api/v1/posts", json=body, headers={"Idempotency-Key": "abc-1"})
    assert first.status_code == second.status_code == 201
    assert second.headers.get("idempotent-replayed") == "true"
    assert first.json()["id"] == second.json()["id"]
    with database.session() as session:
        assert len(session.scalars(select(ContentDraft.id)).all()) == 1
    other = client.post(
        "/api/v1/posts", json={**body, "title": "Twice"}, headers={"Idempotency-Key": "abc-1"}
    )
    assert other.status_code == 409
    assert other.json()["code"] == "idempotency_conflict"


def test_stale_if_match_returns_409_and_changes_nothing(
    client: TestClient, database: Database
) -> None:
    post = _create(client, _account(database, "youtube"))
    path = f"/api/v1/posts/{post['id']}"
    assert client.patch(path, json={"title": "x"}).status_code == 428
    ok = client.patch(path, json={"title": "Renamed"}, headers={"If-Match": f'"{post["version"]}"'})
    assert ok.status_code == 200, ok.text
    stale = client.patch(
        path, json={"body": "overwrite"}, headers={"If-Match": f'"{post["version"]}"'}
    )
    assert stale.status_code == 409
    assert stale.headers["content-type"].startswith("application/problem+json")
    assert stale.json()["code"] == "version_conflict"
    assert client.get(path).json()["body"] == "Community update"


def test_problems_and_pagination(client: TestClient, database: Database) -> None:
    missing = client.get("/api/v1/posts/999")
    assert missing.status_code == 404 and missing.json()["code"] == "not_found"
    bad = client.post("/api/v1/posts", json={"title": ""})
    assert bad.status_code == 422 and bad.json()["code"] == "validation_failed"
    account = _account(database, "youtube")
    ids = [_create(client, account)["id"] for _ in range(3)]
    page = client.get("/api/v1/posts", params={"limit": 2}).json()
    assert [item["id"] for item in page["items"]] == ids[::-1][:2]
    rest = client.get("/api/v1/posts", params={"limit": 2, "cursor": page["next_cursor"]}).json()
    assert [item["id"] for item in rest["items"]] == [ids[0]]
    assert rest["next_cursor"] is None


def test_release_requires_echo_and_confirmation_over_http(
    client: TestClient, database: Database
) -> None:
    post = _create(
        client,
        _account(database, "youtube"),
        planned_at=(datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    target = post["targets"][0]  # type: ignore[index]
    path = f"/api/v1/posts/{post['id']}/targets/{target['id']}/release"
    unconfirmed = client.post(path, json={"payload_hash": target["payload_hash"]})
    assert unconfirmed.status_code == 422
    wrong = client.post(path, json={"payload_hash": "0" * 64, "confirmed": True})
    assert (
        wrong.status_code == 409
        and wrong.json()["current_payload_hash"] == (target["payload_hash"])
    )


def test_tiktok_release_by_an_api_key_is_human_required(
    database: Database, settings: Settings
) -> None:
    account = _account(database, "tiktok")
    app = create_app(settings)
    with TestClient(app, base_url=BASE) as web:
        post = web.post(
            "/api/v1/posts",
            json={
                "title": "Dance",
                "targets": [
                    {
                        "platform": "tiktok",
                        "account_id": account,
                        "post_type": "tiktok_video",
                        "settings": {"privacy_level": "SELF_ONLY"},
                        "planned_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    }
                ],
            },
        ).json()
        target = post["targets"][0]
        app.dependency_overrides[get_principal] = lambda: Principal(
            kind="agent",
            id="key-1",
            workspace_id=1,
            role=None,
            scopes=ALL_SCOPES,
            display="Agent",
        )
        refused = web.post(
            f"/api/v1/posts/{post['id']}/targets/{target['id']}/release",
            json={"payload_hash": target["payload_hash"], "confirmed": True},
        )
    assert refused.status_code == 403
    assert refused.json()["code"] == "human_required"


def test_scopes_are_enforced(database: Database, settings: Settings) -> None:
    app = create_app(settings)
    app.dependency_overrides[get_principal] = lambda: Principal(
        kind="agent",
        id="reader",
        workspace_id=1,
        role=None,
        scopes=frozenset({"posts:read"}),
        display="Reader",
    )
    with TestClient(app, base_url=BASE) as web:
        assert web.get("/api/v1/posts").status_code == 200
        denied = web.post("/api/v1/posts", json={"title": "x"})
    assert denied.status_code == 403 and denied.json()["code"] == "scope_missing"


def test_legacy_drafts_routes_still_answer(client: TestClient) -> None:
    created = client.post("/api/drafts", json={"title": "Legacy", "targets": []})
    assert created.status_code == 200, created.text
    assert client.get("/api/drafts").status_code == 200
    # The same row is a v1 post.
    assert client.get(f"/api/v1/posts/{created.json()['id']}").status_code == 200
