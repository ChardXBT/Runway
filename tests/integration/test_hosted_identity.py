"""Hosted profile end to end: Clerk session tokens, the webhook mirror, the workspace API,
rate limits and hidden local-only routes. Keys, tokens and signatures are generated here;
nothing reaches the network.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from identity_support import (
    ISSUER,
    WEB_ORIGIN,
    WEBHOOK_SECRET,
    FakeJwksFetcher,
    jwks,
    membership_event,
    mint,
    rsa_key,
    session_claims,
    svix_headers,
)
from sqlalchemy import func, select

from runway.api.app import create_app
from runway.api.platform_routes import build_platform_router
from runway.config import Settings
from runway.db import Database, initialize_database
from runway.db.hosted_models import IdentityWebhookDelivery, WorkspaceIdentityLink
from runway.db.models import (
    AuditLogEntry,
    ConnectedAccount,
    ContentDraft,
    User,
    Workspace,
    WorkspaceMember,
)
from runway.hosted.quotas import set_quota_overrides
from runway.identity.registry import build_identity_provider

HOOK = "/hooks/identity/clerk"


@dataclass
class Hosted:
    app: FastAPI
    client: TestClient
    settings: Settings
    database: Database
    fetcher: FakeJwksFetcher

    def token(self, user: str, org: str | None = None, **claims: Any) -> str:
        return mint(rsa_key(), session_claims(user, org, **claims))

    def auth(self, user: str, org: str | None = None, **claims: Any) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token(user, org, **claims)}"}

    def hook(
        self, payload: dict[str, Any], delivery_id: str, *, secret: str = WEBHOOK_SECRET
    ) -> httpx.Response:
        body = json.dumps(payload).encode()
        headers = svix_headers(body, delivery_id, secret=secret)
        return self.client.post(HOOK, content=body, headers=headers)

    def user_id(self, external_id: str) -> int:
        with self.database.session() as session:
            user = session.scalar(select(User).where(User.external_id == external_id))
            assert user is not None
            return user.id

    def count(self, model: Any, *where: Any) -> int:
        with self.database.session() as session:
            return int(session.scalar(select(func.count()).select_from(model).where(*where)) or 0)


def hosted_settings(tmp_path: Path, **overrides: Any) -> Settings:
    base = Settings(
        data_dir=tmp_path / "data",
        web_origins=WEB_ORIGIN,
        clerk_issuer=ISSUER,
        clerk_webhook_secret=WEBHOOK_SECRET,
    )
    # model_copy skips validation: hosted Settings refuse SQLite until Postgres
    # support lands, but everything else here is the real hosted code path.
    return base.model_copy(update={"profile": "hosted", "identity_provider": "clerk", **overrides})


@contextmanager
def hosted_app(tmp_path: Path, **overrides: Any) -> Iterator[Hosted]:
    settings = hosted_settings(tmp_path, **overrides)
    database = initialize_database(settings)
    fetcher = FakeJwksFetcher(jwks((rsa_key(), "kid-1")))
    application = create_app(settings)
    application.state.identity_provider = build_identity_provider(settings, fetcher=fetcher)
    try:
        with TestClient(application, base_url="https://api.runway.test") as client:
            yield Hosted(application, client, settings, database, fetcher)
    finally:
        database.engine.dispose()


@pytest.fixture()
def hosted(tmp_path: Path) -> Iterator[Hosted]:
    with hosted_app(tmp_path) as running:
        yield running


def _problem(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/problem+json")
    body: dict[str, Any] = response.json()
    assert body["code"] == code
    return body


# ------------------------------------------------------------------ authentication


def test_hosted_request_without_a_token_is_401(hosted: Hosted) -> None:
    for path in ("/api/v1/workspace", "/api/v1/workspace/members", "/api/settings"):
        response = hosted.client.get(path)
        _problem(response, 401, "authentication_required")
        assert response.headers["www-authenticate"] == "Bearer"
    assert hosted.client.get("/health").status_code == 200


def test_forged_expired_and_wrong_issuer_tokens_are_401(hosted: Hosted) -> None:
    claims = session_claims("user_a", "org_a")
    cases = {
        "forged": mint(rsa_key("attacker"), claims),
        "expired": mint(rsa_key(), session_claims("user_a", "org_a", lifetime=-60)),
        "wrong issuer": mint(rsa_key(), session_claims("user_a", "org_a", issuer="https://x.test")),
        "wrong origin": mint(rsa_key(), session_claims("user_a", "org_a", azp="https://x.test")),
        "unknown key": mint(rsa_key(), claims, kid="kid-404"),
        "not a jwt": "definitely-not-a-jwt",
    }
    for name, token in cases.items():
        response = hosted.client.get(
            "/api/v1/workspace", headers={"Authorization": f"Bearer {token}"}
        )
        body = _problem(response, 401, "token_expired" if name == "expired" else "invalid_token")
        assert token not in json.dumps(body), name
        assert response.headers["www-authenticate"].startswith("Bearer error=")
    # Nothing was mirrored for any refused token.
    assert hosted.count(User) == 0
    assert hosted.count(WorkspaceIdentityLink) == 0


def test_an_api_key_bearer_still_reaches_the_api_key_seam(hosted: Hosted) -> None:
    response = hosted.client.get(
        "/api/v1/workspace", headers={"Authorization": "Bearer rwy_live_abc_secret"}
    )
    _problem(response, 401, "api_key_unsupported")


def test_identity_provider_outage_is_503_not_401(tmp_path: Path) -> None:
    with hosted_app(tmp_path) as running:
        running.fetcher.error = ConnectionError("clerk is down")
        response = running.client.get("/api/v1/workspace", headers=running.auth("user_a", "org_a"))
        _problem(response, 503, "identity_unavailable")


def test_a_token_without_an_active_organization_is_403(hosted: Hosted) -> None:
    response = hosted.client.get("/api/v1/workspace", headers=hosted.auth("user_a"))
    _problem(response, 403, "workspace_required")
    assert hosted.count(User) == 1  # the user is mirrored even so


# ------------------------------------------------------------------ workspaces and roles


def test_first_admin_of_a_new_organization_becomes_owner(hosted: Hosted) -> None:
    response = hosted.client.get(
        "/api/v1/workspace", headers=hosted.auth("user_a", "org_a", email="a@example.test")
    )
    assert response.status_code == 200, response.text
    workspace = response.json()
    assert workspace["your_role"] == "owner"
    assert workspace["profile"] == "hosted"
    assert workspace["members_managed_by"] == "identity_provider"
    assert workspace["id"] != 1  # workspace 1 is the local owner's
    members = hosted.client.get("/api/v1/workspace/members", headers=hosted.auth("user_a", "org_a"))
    assert [(m["email"], m["role"]) for m in members.json()["members"]] == [
        ("a@example.test", "owner")
    ]
    # v1 org claims resolve to the same workspace.
    again = hosted.client.get(
        "/api/v1/workspace", headers=hosted.auth("user_a", "org_a", form="v1")
    )
    assert again.json()["id"] == workspace["id"]
    assert hosted.count(WorkspaceMember) == 1


def test_a_non_admin_cannot_claim_a_new_workspace(hosted: Hosted) -> None:
    response = hosted.client.get(
        "/api/v1/workspace", headers=hosted.auth("user_m", "org_new", role="member")
    )
    _problem(response, 403, "membership_unknown")
    # The admin can still claim it afterwards.
    owner = hosted.client.get("/api/v1/workspace", headers=hosted.auth("user_a", "org_new"))
    assert owner.json()["your_role"] == "owner"


def test_a_valid_token_for_org_a_cannot_read_workspace_b(hosted: Hosted) -> None:
    a = hosted.client.get("/api/v1/workspace", headers=hosted.auth("user_a", "org_a")).json()
    b = hosted.client.get("/api/v1/workspace", headers=hosted.auth("user_b", "org_b")).json()
    assert a["id"] != b["id"]
    user_b = hosted.user_id("user_b")

    members = hosted.client.get("/api/v1/workspace/members", headers=hosted.auth("user_a", "org_a"))
    assert [m["user_id"] for m in members.json()["members"]] == [hosted.user_id("user_a")]
    cross = hosted.client.patch(
        f"/api/v1/workspace/members/{user_b}",
        json={"role": "viewer"},
        headers=hosted.auth("user_a", "org_a"),
    )
    _problem(cross, 404, "member_not_found")
    # A session whose active organization is B, for a user Runway has no B
    # membership for, gets nothing from B, even claiming to be its admin.
    for role in ("member", "admin"):
        response = hosted.client.get(
            "/api/v1/workspace", headers=hosted.auth("user_a", "org_b", role=role)
        )
        _problem(response, 403, "membership_unknown")
    with hosted.database.session() as session:
        role_b = session.scalar(
            select(WorkspaceMember.role).where(WorkspaceMember.user_id == user_b)
        )
    assert role_b == "owner"


def test_an_editor_cannot_change_roles(hosted: Hosted) -> None:
    hosted.client.get("/api/v1/workspace", headers=hosted.auth("user_a", "org_a"))
    assert hosted.hook(membership_event("created", "org_a", "user_e"), "msg_e").status_code == 200
    editor_id = hosted.user_id("user_e")
    viewer = hosted.client.get(
        "/api/v1/workspace", headers=hosted.auth("user_e", "org_a", role="member")
    )
    assert viewer.json()["your_role"] == "viewer"  # the configured default role

    promoted = hosted.client.patch(
        f"/api/v1/workspace/members/{editor_id}",
        json={"role": "editor"},
        headers=hosted.auth("user_a", "org_a"),
    )
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["role"] == "editor"

    as_editor = hosted.auth("user_e", "org_a", role="member")
    for target in (hosted.user_id("user_a"), editor_id):
        response = hosted.client.patch(
            f"/api/v1/workspace/members/{target}", json={"role": "owner"}, headers=as_editor
        )
        _problem(response, 403, "scope_missing")
    audit = hosted.count(AuditLogEntry, AuditLogEntry.action == "workspace.member_role_changed")
    assert audit == 1


def test_the_last_owner_cannot_be_demoted(hosted: Hosted) -> None:
    owner = hosted.auth("user_a", "org_a")
    hosted.client.get("/api/v1/workspace", headers=owner)
    hosted.hook(membership_event("created", "org_a", "user_e"), "msg_e")
    owner_id, other_id = hosted.user_id("user_a"), hosted.user_id("user_e")
    demote = hosted.client.patch(
        f"/api/v1/workspace/members/{owner_id}", json={"role": "editor"}, headers=owner
    )
    _problem(demote, 409, "last_owner")
    for role in ("bogus", "OWNER"):
        bad = hosted.client.patch(
            f"/api/v1/workspace/members/{other_id}", json={"role": role}, headers=owner
        )
        assert bad.status_code == 422
    hosted.client.patch(
        f"/api/v1/workspace/members/{other_id}", json={"role": "owner"}, headers=owner
    )
    demote = hosted.client.patch(
        f"/api/v1/workspace/members/{owner_id}", json={"role": "editor"}, headers=owner
    )
    assert demote.status_code == 200
    assert demote.json()["role"] == "editor"


# ------------------------------------------------------------------ webhooks


def test_webhook_with_a_bad_signature_is_rejected(hosted: Hosted) -> None:
    payload = membership_event("created", "org_a", "user_x")
    body = json.dumps(payload).encode()
    wrong_secret = hosted.hook(payload, "msg_1", secret="whsec_" + "QUFBQUFBQUFBQUFBQUFBQQ==")
    _problem(wrong_secret, 401, "webhook_signature_invalid")
    tampered_headers = svix_headers(body, "msg_2")
    tampered = hosted.client.post(
        HOOK, content=body.replace(b"user_x", b"user_y"), headers=tampered_headers
    )
    _problem(tampered, 401, "webhook_signature_invalid")
    stale = hosted.client.post(HOOK, content=body, headers=svix_headers(body, "msg_3", timestamp=1))
    _problem(stale, 401, "webhook_signature_invalid")
    unsigned = hosted.client.post(HOOK, content=body, headers={"content-type": "application/json"})
    _problem(unsigned, 401, "webhook_signature_invalid")
    assert hosted.count(IdentityWebhookDelivery) == 0
    assert hosted.count(WorkspaceMember) == 0


def test_webhook_mirrors_a_membership_once_even_when_replayed(hosted: Hosted) -> None:
    payload = membership_event(
        "created", "org_a", "user_x", role="org:admin", email="x@example.test"
    )
    first = hosted.hook(payload, "msg_same")
    assert first.status_code == 200, first.text
    assert first.json()["outcome"] == "applied"
    # Svix re-signs a retry with a new timestamp but keeps the message id.
    for _ in range(3):
        replay = hosted.hook(payload, "msg_same")
        assert replay.status_code == 200
        assert replay.json()["outcome"] == "duplicate"
    user_x = hosted.user_id("user_x")
    assert hosted.count(WorkspaceMember, WorkspaceMember.user_id == user_x) == 1
    assert hosted.count(IdentityWebhookDelivery) == 1
    assert hosted.count(AuditLogEntry, AuditLogEntry.action == "identity.member_added") == 1
    # The first admin delivered by webhook owns the new workspace.
    response = hosted.client.get("/api/v1/workspace", headers=hosted.auth("user_x", "org_a"))
    assert response.json()["your_role"] == "owner"
    assert response.json()["name"] == "Org org_a"


def test_membership_removal_revokes_access_but_keeps_the_workspace(hosted: Hosted) -> None:
    owner = hosted.auth("user_a", "org_a")
    workspace_id = hosted.client.get("/api/v1/workspace", headers=owner).json()["id"]
    hosted.hook(membership_event("created", "org_a", "user_e"), "msg_add")
    member = hosted.auth("user_e", "org_a", role="member")
    assert hosted.client.get("/api/v1/workspace", headers=member).status_code == 200

    removed = hosted.hook(membership_event("deleted", "org_a", "user_e"), "msg_remove")
    assert removed.json()["outcome"] == "applied"
    _problem(hosted.client.get("/api/v1/workspace", headers=member), 403, "membership_unknown")
    # Signing in again does not resurrect the membership.
    _problem(hosted.client.get("/api/v1/workspace", headers=member), 403, "membership_unknown")
    with hosted.database.session() as session:
        assert session.get(Workspace, workspace_id) is not None
    assert hosted.client.get("/api/v1/workspace", headers=owner).status_code == 200


def test_an_older_event_does_not_undo_a_newer_one(hosted: Hosted) -> None:
    hosted.client.get("/api/v1/workspace", headers=hosted.auth("user_a", "org_a"))
    later = membership_event("deleted", "org_a", "user_y", timestamp_ms=2_000_000_000_000)
    earlier = membership_event("created", "org_a", "user_y", timestamp_ms=1_900_000_000_000)
    assert hosted.hook(later, "msg_later").json()["outcome"] == "applied"
    assert hosted.hook(earlier, "msg_earlier").json()["outcome"] == "stale"
    assert hosted.count(WorkspaceMember) == 1  # only user_a


def test_organization_and_user_deletion_revoke_memberships(hosted: Hosted) -> None:
    owner = hosted.auth("user_a", "org_a")
    workspace_id = hosted.client.get("/api/v1/workspace", headers=owner).json()["id"]
    hosted.client.get("/api/v1/workspace", headers=hosted.auth("user_b", "org_b"))
    deleted_org = {"type": "organization.deleted", "data": {"id": "org_a", "deleted": True}}
    assert hosted.hook(deleted_org, "msg_org").json()["outcome"] == "applied"
    _problem(hosted.client.get("/api/v1/workspace", headers=owner), 403, "workspace_unavailable")
    with hosted.database.session() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None and workspace.deleted_at is None
    deleted_user = {"type": "user.deleted", "data": {"id": "user_b", "deleted": True}}
    assert hosted.hook(deleted_user, "msg_user").json()["outcome"] == "applied"
    assert hosted.count(WorkspaceMember) == 0
    ignored = hosted.hook({"type": "email.created", "data": {}}, "msg_other")
    assert ignored.json()["outcome"] == "ignored"
    bad = hosted.hook({"type": "organizationMembership.created", "data": {}}, "msg_bad")
    _problem(bad, 400, "webhook_payload_invalid")


def test_user_webhooks_fill_in_email_and_name(hosted: Hosted) -> None:
    hosted.client.get("/api/v1/workspace", headers=hosted.auth("user_a", "org_a"))
    hosted.hook(
        {
            "type": "user.updated",
            "data": {
                "id": "user_a",
                "primary_email_address_id": "idn_1",
                "email_addresses": [{"id": "idn_1", "email_address": "ann@example.test"}],
                "first_name": "Ann",
                "last_name": "Lee",
            },
        },
        "msg_user",
    )
    members = hosted.client.get("/api/v1/workspace/members", headers=hosted.auth("user_a", "org_a"))
    assert members.json()["members"][0]["display_name"] == "Ann Lee"
    assert members.json()["members"][0]["email"] == "ann@example.test"


# ------------------------------------------------------------------ hardening


def test_rate_limits_return_429_problem_json(tmp_path: Path) -> None:
    with hosted_app(tmp_path / "principal", rate_limit_principal_per_minute=2) as running:
        headers = running.auth("user_a", "org_a")
        statuses = [
            running.client.get("/api/v1/workspace", headers=headers).status_code for _ in range(3)
        ]
        assert statuses[:2] == [200, 200]
        response = running.client.get("/api/v1/workspace", headers=headers)
        _problem(response, 429, "rate_limited")
        assert int(response.headers["retry-after"]) >= 1

    with hosted_app(tmp_path / "ip", rate_limit_ip_per_minute=2) as running:
        for _ in range(2):
            assert running.client.get("/api/v1/workspace").status_code == 401
        _problem(running.client.get("/api/v1/workspace"), 429, "rate_limited")
        assert running.client.get("/health").status_code == 200  # probes are exempt

    with hosted_app(tmp_path / "workspace") as running:
        headers = running.auth("user_a", "org_a")
        workspace_id = running.client.get("/api/v1/workspace", headers=headers).json()["id"]
        with running.database.session() as session:
            set_quota_overrides(session, workspace_id, updated_by="test", api_requests_per_minute=1)
        running.app.state.rate_limiter._buckets.clear()
        assert running.client.get("/api/v1/workspace", headers=headers).status_code == 200
        _problem(running.client.get("/api/v1/workspace", headers=headers), 429, "rate_limited")


def test_hosted_hides_the_local_only_routes(hosted: Hosted) -> None:
    headers = hosted.auth("user_a", "org_a")
    hidden = [
        ("GET", "/api/queue"),
        ("GET", "/api/catalog"),
        ("GET", "/api/capture/runs"),
        ("GET", "/api/profiles"),
        ("GET", "/api/intelligence/status"),
        ("GET", "/api/proposals"),
        ("GET", "/api/publisher/queue"),
        ("POST", "/api/publisher/connectors/validate"),
        ("GET", "/api/search-runs/1"),
        ("GET", "/api/settings/full"),
        ("GET", "/api/activity"),
        ("GET", "/media/historical/x.png"),
        ("GET", "/docs"),
        ("GET", "/openapi.json"),
    ]
    for method, path in hidden:
        response = hosted.client.request(method, path, headers=headers)
        assert response.status_code == 404, (method, path)
    # Shared surfaces stay.
    view = hosted.client.get("/api/settings", headers=headers)
    assert view.status_code == 200
    assert view.json()["profile"] == "hosted"
    assert not {"data_dir", "connector_account_email", "publisher_channel_id"} & set(view.json())


def _platform_route_requests(settings: Settings, database: Database) -> list[tuple[str, str]]:
    """Every (method, concrete path) the legacy platform router serves."""
    filled = {"platform": "youtube", "account_id": "1", "run_id": "1", "content_id": "1"}
    filled |= {"comment_id": "1", "draft_id": "1", "target_id": "1"}
    requests: list[tuple[str, str]] = []
    for route in build_platform_router(database, settings).routes:
        assert isinstance(route, APIRoute)
        path = route.path
        for name, value in filled.items():
            path = path.replace("{" + name + "}", value)
        assert "{" not in path, path
        requests.extend((method, path) for method in sorted(route.methods))
    return requests


def test_hosted_does_not_serve_the_unscoped_platform_routes(hosted: Hosted) -> None:
    # The platform router reads and writes the global tables with no workspace
    # filter and no scope checks: a viewer of workspace B could disconnect
    # workspace A's account. Hosted must not serve any of it until tenancy lands.
    with hosted.database.session() as session:
        session.add(
            ConnectedAccount(platform="youtube", external_account_id="UC_a", status="connected")
        )
    hosted.hook(membership_event("created", "org_b", "user_b"), "msg_b")
    owner_a = hosted.auth("user_a", "org_a")
    viewer_b = hosted.auth("user_b", "org_b", role="member")
    assert hosted.client.get("/api/v1/workspace", headers=owner_a).status_code == 200
    viewer = hosted.client.get("/api/v1/workspace", headers=viewer_b)
    assert viewer.json()["your_role"] == "viewer"

    requests = _platform_route_requests(hosted.settings, hosted.database)
    assert len(requests) > 25
    for headers in (owner_a, viewer_b):
        for method, path in requests:
            body = {"confirmed": True} if method != "GET" else None
            response = hosted.client.request(method, path, headers=headers, json=body)
            assert response.status_code == 404, (method, path, response.text)

    with hosted.database.session() as session:
        account = session.scalar(select(ConnectedAccount))
        assert account is not None and account.status == "connected"
        assert session.scalar(select(func.count()).select_from(ContentDraft)) == 0


def test_local_profile_still_serves_platform_routes_without_login(
    settings: Settings, database: Database
) -> None:
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        for path in ("/api/platforms", "/api/platforms/accounts", "/api/inbox", "/api/drafts"):
            response = client.get(path)
            assert response.status_code == 200, (path, response.text)


def test_cookie_sessions_need_the_web_origin_for_state_changes(hosted: Hosted) -> None:
    hosted.client.get("/api/v1/workspace", headers=hosted.auth("user_a", "org_a"))
    hosted.hook(membership_event("created", "org_a", "user_e"), "msg_e")
    target = f"/api/v1/workspace/members/{hosted.user_id('user_e')}"
    cookie = {"cookie": f"__session={hosted.token('user_a', 'org_a')}"}
    body = {"role": "editor"}

    _problem(hosted.client.patch(target, json=body, headers=cookie), 403, "origin_required")
    foreign = {**cookie, "origin": "https://evil.example"}
    _problem(hosted.client.patch(target, json=body, headers=foreign), 403, "cross_origin_refused")
    loopback = {**cookie, "origin": "http://127.0.0.1:3000"}
    _problem(hosted.client.patch(target, json=body, headers=loopback), 403, "cross_origin_refused")
    ok = hosted.client.patch(target, json=body, headers={**cookie, "origin": WEB_ORIGIN})
    assert ok.status_code == 200, ok.text
    # Reads by cookie and bearer writes without an Origin are fine.
    assert hosted.client.get("/api/v1/workspace", headers=cookie).status_code == 200
    bearer = hosted.client.patch(
        target, json={"role": "viewer"}, headers=hosted.auth("user_a", "org_a")
    )
    assert bearer.status_code == 200


def test_security_headers_and_cors_use_the_web_origin(hosted: Hosted) -> None:
    for response in (hosted.client.get("/health"), hosted.client.get("/api/v1/workspace")):
        assert response.headers["strict-transport-security"].startswith("max-age=")
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["cache-control"] == "no-store"
    preflight = hosted.client.options(
        "/api/v1/workspace/members/1",
        headers={"origin": WEB_ORIGIN, "access-control-request-method": "PATCH"},
    )
    assert preflight.headers["access-control-allow-origin"] == WEB_ORIGIN
    assert preflight.headers["access-control-allow-credentials"] == "true"
    loopback = hosted.client.options(
        "/api/v1/workspace/members/1",
        headers={"origin": "http://127.0.0.1:3000", "access-control-request-method": "PATCH"},
    )
    assert "access-control-allow-origin" not in loopback.headers


# ------------------------------------------------------------------ local profile


def test_local_workspace_api_needs_no_login(settings: Settings, database: Database) -> None:
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
        workspace = client.get("/api/v1/workspace")
        assert workspace.status_code == 200, workspace.text
        assert workspace.json()["id"] == 1
        assert workspace.json()["your_role"] == "owner"
        assert workspace.json()["members_managed_by"] == "local_owner"
        assert client.get("/api/v1/workspace/members").json() == {"members": []}
        _problem(
            client.patch("/api/v1/workspace/members/7", json={"role": "editor"}),
            404,
            "member_not_found",
        )
        # The hosted-only webhook route does not exist locally.
        assert client.post(HOOK, content=b"{}").status_code == 404
        assert "strict-transport-security" not in workspace.headers
