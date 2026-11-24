"""`GET /api/v1/accounts/{id}/publishing/capability`: per-post-type words.

The accounts page shows "capability per post type in words" (spec 08 section 5)
without forcing the owner to open a post editor first: the route reuses the
same capability rule the post service applies to a target.
"""

from __future__ import annotations

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from runway.api.v1.accounts_routes import build_accounts_router
from runway.api.v1.errors import install_problem_handlers
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ConnectedAccount

BASE = "http://127.0.0.1:8000"


@pytest.fixture()
def client(database: Database, settings: Settings) -> TestClient:
    app = FastAPI()
    app.state.settings = settings
    install_problem_handlers(app)
    app.include_router(build_accounts_router(database, settings))
    return TestClient(app, base_url=BASE)


_SEQ = iter(range(10_000))


def _account(
    database: Database,
    *,
    platform: str = "youtube",
    scopes: list[str] | None = None,
    status: str = "connected",
) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform=platform,
            external_account_id=f"{platform}-owner-{next(_SEQ)}",
            handle="@owner",
            status=status,
            granted_scopes_json=json.dumps([] if scopes is None else scopes),
            token_ref=None,
            metadata_json="{}",
        )
        session.add(account)
        session.flush()
        return account.id


def test_capability_lists_every_post_type_of_the_account_platform(
    client: TestClient, database: Database
) -> None:
    account_id = _account(database, platform="youtube")
    response = client.get(f"/api/v1/accounts/{account_id}/publishing/capability")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["account_id"] == account_id
    assert body["platform"] == "youtube"
    post_types = {item["post_type"] for item in body["items"]}
    assert post_types == {"youtube_video", "youtube_short", "youtube_post"}
    for item in body["items"]:
        assert item["state"] in {
            "ready",
            "ready_draft_only",
            "needs_scope",
            "needs_app_approval",
            "needs_media_host",
            "assisted_only",
        }
        assert isinstance(item["detail"], str) and item["detail"]
        assert isinstance(item["missing_scopes"], list)
        assert isinstance(item["available_publishers"], list)


def test_capability_marks_scoped_post_types_ready(client: TestClient, database: Database) -> None:
    account_id = _account(
        database, platform="youtube", scopes=["https://www.googleapis.com/auth/youtube.upload"]
    )
    body = client.get(f"/api/v1/accounts/{account_id}/publishing/capability").json()
    by_type = {item["post_type"]: item for item in body["items"]}
    assert by_type["youtube_video"]["state"] == "ready"
    # Without the upload scope nothing is ready.
    unscoped = _account(database, platform="youtube")
    body = client.get(f"/api/v1/accounts/{unscoped}/publishing/capability").json()
    by_type = {item["post_type"]: item for item in body["items"]}
    assert by_type["youtube_video"]["state"] == "needs_scope"
    assert by_type["youtube_video"]["missing_scopes"]


def test_capability_lists_tiktok_and_instagram_post_types(
    client: TestClient, database: Database
) -> None:
    tiktok = _account(database, platform="tiktok")
    body = client.get(f"/api/v1/accounts/{tiktok}/publishing/capability").json()
    assert {item["post_type"] for item in body["items"]} == {"tiktok_video", "tiktok_draft"}
    instagram = _account(database, platform="instagram")
    body = client.get(f"/api/v1/accounts/{instagram}/publishing/capability").json()
    assert {item["post_type"] for item in body["items"]} == {
        "instagram_reel",
        "instagram_image",
        "instagram_carousel",
        "instagram_story",
    }


def test_capability_404s_for_a_missing_account(client: TestClient) -> None:
    response = client.get("/api/v1/accounts/424242/publishing/capability")
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
