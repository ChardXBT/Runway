"""API keys, scopes, policy and per-key limits (spec 06 sections 3-5) on a temporary database."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from runway.api.app import create_app
from runway.auth.api_keys import DEFAULT_AGENT_SCOPES, ApiKeyService, hash_key
from runway.auth.key_limits import enforce_daily_post_cap
from runway.auth.principal import ALL_SCOPES, local_owner
from runway.auth.problems import Problem
from runway.cli import keys_commands
from runway.cli.main import app as cli_app
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ApiKey, AuditLogEntry, Channel, ContentDraft, Proposal

KEY_PATTERN = re.compile(r"^rwy_local_[0-9a-f]{8}_[A-Za-z0-9_-]{32,}$")
BASE_URL = "http://127.0.0.1:8000"


def _client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url=BASE_URL) as client:
        yield client


@pytest.fixture()
def client(settings: Settings, database: Database) -> Iterator[TestClient]:
    yield from _client(settings)


def _bearer(secret: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {secret}"}


def _create(client: TestClient, **body: object) -> dict[str, object]:
    response = client.post("/api/v1/keys", json={"name": "Muse", **body})
    assert response.status_code == 201, response.text
    payload: dict[str, object] = response.json()
    return payload


def _audit_rows(database: Database) -> list[AuditLogEntry]:
    with database.session() as session:
        rows = list(session.scalars(select(AuditLogEntry).order_by(AuditLogEntry.id)).all())
        for row in rows:
            session.expunge(row)
        return rows


# ------------------------------------------------------------------ creation


def test_creating_a_key_shows_the_secret_once_and_stores_only_a_hash(
    client: TestClient, database: Database
) -> None:
    created = _create(client)
    secret = str(created["secret"])
    assert KEY_PATTERN.match(secret)
    key = created["key"]
    assert isinstance(key, dict)
    assert "secret" not in key and "key_hash" not in key
    assert secret.split("_")[2] == key["prefix"]
    with database.session() as session:
        row = session.scalars(select(ApiKey)).one()
        assert row.key_hash == hash_key(secret)
        assert secret not in (row.key_hash, row.prefix, row.scopes_json, row.name)
    listed = client.get("/api/v1/keys").json()
    assert secret not in json.dumps(listed)
    assert [item["id"] for item in listed["items"]] == [key["id"]]


def test_default_agent_scopes_are_exactly_the_spec_list(client: TestClient) -> None:
    key = _create(client)["key"]
    assert isinstance(key, dict)
    assert set(key["scopes"]) == {
        "accounts:read",
        "content:read",
        "inbox:read",
        "inbox:draft",
        "media:write",
        "posts:read",
        "posts:write",
        "posts:schedule",
    }
    assert set(key["scopes"]) == DEFAULT_AGENT_SCOPES
    assert not {"posts:release", "inbox:reply", "accounts:manage", "workspace:admin"} & set(
        key["scopes"]
    )


def test_a_key_can_never_hold_workspace_admin(client: TestClient) -> None:
    """A key holding workspace:admin could administer credentials itself."""
    refused = client.post("/api/v1/keys", json={"name": "admin", "scopes": ["workspace:admin"]})
    assert refused.status_code == 422
    assert refused.json()["code"] == "validation_failed"
    mixed = client.post(
        "/api/v1/keys", json={"name": "mix", "scopes": ["workspace:admin", "posts:read"]}
    )
    assert mixed.status_code == 422
    assert mixed.json()["code"] == "validation_failed"


def test_key_administration_is_for_people_only(client: TestClient) -> None:
    """A key can never list, inspect, mint, widen or revoke credentials (invariant 16).

    workspace:admin is never grantable, so the refusal is `scope_missing`
    before the human check is even reached.
    """
    agent = str(_create(client)["secret"])
    victim = _create(client)["key"]
    assert isinstance(victim, dict)
    for method, url, body in (
        ("post", "/api/v1/keys", {"name": "spawned"}),
        ("get", "/api/v1/keys", None),
        ("get", f"/api/v1/keys/{victim['id']}", None),
        ("delete", f"/api/v1/keys/{victim['id']}", None),
        (
            "put",
            f"/api/v1/keys/{victim['id']}/release-grants",
            {"grants": [{"platform": "youtube"}]},
        ),
    ):
        response = client.request(method, url, json=body, headers=_bearer(agent))
        assert response.status_code == 403, (method, url, response.text)
        assert response.json()["code"] == "scope_missing"
    # The same calls by the owner keep working.
    assert client.get("/api/v1/keys").status_code == 200
    assert client.get(f"/api/v1/keys/{victim['id']}").status_code == 200
    assert client.delete(f"/api/v1/keys/{victim['id']}").status_code == 200


def test_creating_a_key_needs_workspace_admin(client: TestClient) -> None:
    agent = str(_create(client)["secret"])
    response = client.post("/api/v1/keys", json={"name": "x"}, headers=_bearer(agent))
    assert response.status_code == 403
    assert response.json()["code"] == "scope_missing"
    assert client.get("/api/v1/keys", headers=_bearer(agent)).status_code == 403


def test_unknown_scopes_and_past_expiry_are_refused(client: TestClient) -> None:
    unknown = client.post("/api/v1/keys", json={"name": "x", "scopes": ["posts:delete"]})
    assert unknown.status_code == 422
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    assert client.post("/api/v1/keys", json={"name": "x", "expires_at": past}).status_code == 422


# ------------------------------------------------------------ authentication


def test_a_key_resolves_to_an_agent_principal_and_records_last_use(
    client: TestClient, database: Database
) -> None:
    created = _create(client, scopes=["posts:read"])
    secret = str(created["secret"])
    response = client.get("/api/v1/approvals", headers=_bearer(secret))
    assert response.status_code == 200
    with database.session() as session:
        row = session.scalars(select(ApiKey)).one()
        assert row.last_used_at is not None


@pytest.mark.parametrize(
    "credential",
    [
        "rwy_local_abcdefgh_" + "x" * 43,
        "rwy_live_abcdefgh_" + "x" * 43,
        "rwy_local_short",
    ],
)
def test_unknown_or_malformed_keys_are_401_never_the_owner(
    client: TestClient, credential: str
) -> None:
    response = client.get("/api/v1/keys", headers=_bearer(credential))
    assert response.status_code == 401
    assert response.json()["code"] == "authentication_required"


def test_a_tampered_secret_with_a_real_prefix_is_refused(client: TestClient) -> None:
    secret = str(_create(client, scopes=["posts:read"])["secret"])
    tampered = secret[:-4] + ("aaaa" if not secret.endswith("aaaa") else "bbbb")
    assert client.get("/api/v1/keys", headers=_bearer(tampered)).status_code == 401


def test_revoked_and_expired_keys_stop_working(client: TestClient, database: Database) -> None:
    created = _create(client, scopes=["posts:read"])
    secret = str(created["secret"])
    key = created["key"]
    assert isinstance(key, dict)
    assert client.get("/api/v1/approvals", headers=_bearer(secret)).status_code == 200
    revoked = client.delete(f"/api/v1/keys/{key['id']}")
    assert revoked.status_code == 200
    assert revoked.json()["state"] == "revoked"
    assert client.get("/api/v1/approvals", headers=_bearer(secret)).status_code == 401

    other = _create(client, scopes=["posts:read"])
    other_key = other["key"]
    assert isinstance(other_key, dict)
    with database.session() as session:
        row = session.get(ApiKey, other_key["id"])
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    expired = client.get("/api/v1/approvals", headers=_bearer(str(other["secret"])))
    assert expired.status_code == 401
    assert "expired" in expired.json()["detail"]


def test_keys_are_refused_on_unversioned_routes(client: TestClient) -> None:
    secret = str(_create(client)["secret"])
    response = client.get("/api/settings", headers=_bearer(secret))
    assert response.status_code == 403
    assert response.json()["code"] == "scope_missing"
    # Without a key the same route keeps today's owner trust (invariant 17).
    assert client.get("/api/settings").status_code == 200


def test_requests_without_a_key_keep_owner_trust_in_local(client: TestClient) -> None:
    assert client.get("/api/v1/keys").status_code == 200
    assert client.get("/api/v1/policy").json()["agent_release"] == "never"


def test_the_per_key_rate_limit_answers_429_with_retry_after(
    settings: Settings, database: Database
) -> None:
    limited = settings.model_copy(update={"api_key_rate_limit_per_minute": 2})
    for client in _client(limited):
        secret = str(_create(client, scopes=["posts:read"])["secret"])
        assert client.get("/api/v1/approvals", headers=_bearer(secret)).status_code == 200
        assert client.get("/api/v1/approvals", headers=_bearer(secret)).status_code == 200
        third = client.get("/api/v1/approvals", headers=_bearer(secret))
        assert third.status_code == 429
        assert third.json()["code"] == "rate_limited"
        assert int(third.headers["retry-after"]) >= 1
        # Other principals are not affected.
        assert client.get("/api/v1/approvals").status_code == 200


# --------------------------------------------------------------------- audit


def test_every_mutation_by_a_key_writes_an_audit_row_with_key_id_and_name(
    client: TestClient, database: Database
) -> None:
    admin = _create(client, name="Muse writer", scopes=["posts:write"])
    admin_key = admin["key"]
    assert isinstance(admin_key, dict)
    before = len(_audit_rows(database))
    response = client.post(
        "/api/v1/posts",
        json={"title": "By a key", "body": "text", "targets": []},
        headers=_bearer(str(admin["secret"])),
    )
    assert response.status_code == 201, response.text
    rows = _audit_rows(database)[before:]
    http_rows = [row for row in rows if row.action == "POST /api/v1/posts"]
    assert len(http_rows) == 1
    detail = json.loads(http_rows[0].detail_json)
    assert http_rows[0].actor_kind == "agent"
    assert http_rows[0].actor_id == str(admin_key["id"])
    assert detail["key_id"] == str(admin_key["id"])
    assert detail["key_name"] == "Muse writer"
    # A failed mutation and a read write nothing.
    count = len(_audit_rows(database))
    client.post("/api/v1/posts", json={"title": ""}, headers=_bearer(str(admin["secret"])))
    client.get("/api/v1/approvals", headers=_bearer(str(admin["secret"])))
    assert len(_audit_rows(database)) == count


# -------------------------------------------------------------------- policy


def test_policy_and_release_grants_are_for_people_only(client: TestClient) -> None:
    # No key can hold workspace:admin, so the refusal is scope_missing before
    # the person check; the routes refuse keys twice over.
    agent = str(_create(client)["secret"])
    response = client.put(
        "/api/v1/policy", json={"agent_release": "per_key"}, headers=_bearer(agent)
    )
    assert response.status_code == 403
    assert response.json()["code"] == "scope_missing"
    key = _create(client)["key"]
    assert isinstance(key, dict)
    grants = client.put(
        f"/api/v1/keys/{key['id']}/release-grants",
        json={"grants": [{"platform": "youtube"}]},
        headers=_bearer(agent),
    )
    assert grants.status_code == 403
    assert grants.json()["code"] == "scope_missing"


def test_posts_release_needs_the_per_key_policy_and_never_covers_tiktok(
    client: TestClient,
) -> None:
    refused = client.post(
        "/api/v1/keys",
        json={
            "name": "r",
            "scopes": ["posts:release"],
            "release_grants": [{"platform": "youtube"}],
        },
    )
    assert refused.status_code == 422
    assert client.put("/api/v1/policy", json={"agent_release": "per_key"}).status_code == 200
    tiktok = client.post(
        "/api/v1/keys",
        json={"name": "t", "scopes": ["posts:release"], "release_grants": [{"platform": "tiktok"}]},
    )
    assert tiktok.status_code == 403
    assert tiktok.json()["code"] == "human_required"
    created = _create(
        client,
        scopes=["posts:read", "posts:release"],
        release_grants=[{"platform": "youtube", "daily_cap": 2}],
    )
    key = created["key"]
    assert isinstance(key, dict)
    assert "posts:release" in key["effective_scopes"]
    assert key["release_grants"] == [{"platform": "youtube", "daily_cap": 2}]
    # Turning the policy back to never takes release away from every key at once.
    assert client.put("/api/v1/policy", json={"agent_release": "never"}).status_code == 200
    view = client.get(f"/api/v1/keys/{key['id']}").json()
    assert "posts:release" in view["scopes"]
    assert "posts:release" not in view["effective_scopes"]


# --------------------------------------------------------------- idempotency


def test_idempotent_replay_returns_the_first_result_and_a_new_body_conflicts(
    client: TestClient,
) -> None:
    headers = {"Idempotency-Key": "policy-1"}
    first = client.put("/api/v1/policy", json={"agent_release": "per_key"}, headers=headers)
    assert first.status_code == 200
    # Someone changes the policy in between; the replay still returns the first result.
    assert client.put("/api/v1/policy", json={"agent_release": "never"}).status_code == 200
    again = client.put("/api/v1/policy", json={"agent_release": "per_key"}, headers=headers)
    assert again.status_code == 200
    assert again.headers.get("idempotent-replayed") == "true"
    assert again.json() == first.json()
    assert client.get("/api/v1/policy").json()["agent_release"] == "never"
    conflict = client.put("/api/v1/policy", json={"agent_release": "never"}, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "idempotency_conflict"


# ------------------------------------------------------------ daily post cap


def test_the_daily_post_cap_counts_posts_the_key_created(
    settings: Settings, database: Database
) -> None:
    """The cap reads `content_drafts` rows by this key today — not audit rows,
    so a create path that writes no audit row is still counted."""
    service = ApiKeyService(database, settings)
    created = service.create(local_owner(), name="capped", scopes=["posts:write"])
    principal = service.authenticate(created.secret)
    with database.session() as session:
        for _ in range(2):
            session.add(
                ContentDraft(
                    workspace_id=principal.workspace_id,
                    title="t",
                    body="b",
                    created_by=f"agent:{principal.id}",
                )
            )
        session.flush()
    with database.session() as session, pytest.raises(Problem) as raised:
        enforce_daily_post_cap(session, principal, 2)
    assert raised.value.code == "rate_limited"
    # A bigger cap still passes, and posts by someone else do not count.
    with database.session() as session:
        enforce_daily_post_cap(session, principal, 3)
        session.add(
            ContentDraft(
                workspace_id=principal.workspace_id,
                title="other",
                body="b",
                created_by="user:local-owner",
            )
        )
        session.flush()
        enforce_daily_post_cap(session, principal, 3)
    # People are never capped.
    with database.session() as session:
        enforce_daily_post_cap(session, local_owner(), 0)


def test_the_daily_post_cap_covers_the_rest_and_cross_post_paths(
    settings: Settings, database: Database
) -> None:
    """Cap 1: the second create by one key is refused however it arrives —
    REST, cross-post from a proposal, or a second REST call."""
    capped = settings.model_copy(update={"api_key_daily_post_cap": 1})
    for client in _client(capped):
        service = ApiKeyService(database, capped)
        secret = service.create(local_owner(), name="capped", scopes=["posts:write"]).secret
        headers = _bearer(secret)
        first = client.post(
            "/api/v1/posts",
            json={"title": "one", "body": "b", "targets": []},
            headers=headers,
        )
        assert first.status_code == 201, first.text
        again = client.post(
            "/api/v1/posts",
            json={"title": "two", "body": "b", "targets": []},
            headers=headers,
        )
        assert again.status_code == 429
        assert again.json()["code"] == "rate_limited"
        # A cross-post counts the same domain rows: even though its audit
        # action differs, the second create by this key is refused.
        with database.session() as session:
            session.connection().exec_driver_sql("PRAGMA foreign_keys=OFF")
            channel_id = session.scalar(select(Channel.id).limit(1))
            proposal = Proposal(
                generation_run_id=1,
                channel_id=channel_id,
                candidate_image_id=1,
                planned_publish_at="2026-12-01T09:00:00",
                recommended_caption="cap",
                alternative_captions_json="[]",
                final_caption="cap",
                status="approved",
                selection_reason="r",
                style_score=1.0,
                novelty_score=1.0,
                quality_score=1.0,
            )
            session.add(proposal)
            session.flush()
            proposal_id = proposal.id
        crossed = client.post(
            f"/api/v1/proposals/{proposal_id}/cross-post",
            json={
                "targets": [
                    {
                        "platform": "instagram",
                        "account_id": 1,
                        "post_type": "instagram_image",
                    }
                ]
            },
            headers=headers,
        )
        assert crossed.status_code == 429
        assert crossed.json()["code"] == "rate_limited"
        # And the owner is unaffected by the key's cap.
        by_owner = client.post("/api/v1/posts", json={"title": "owner", "body": "b", "targets": []})
        assert by_owner.status_code == 201, by_owner.text


# ----------------------------------------------------------------------- CLI


def test_cli_creates_lists_and_revokes_keys_and_prints_the_secret_once(
    settings: Settings, database: Database, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(keys_commands, "get_settings", lambda: settings)
    runner = CliRunner()
    created = runner.invoke(cli_app, ["keys", "create", "--name", "Muse", "--scope", "posts:read"])
    assert created.exit_code == 0, created.output
    secrets_printed = re.findall(r"rwy_local_[0-9a-f]{8}_[A-Za-z0-9_-]{32,}", created.stdout)
    assert len(secrets_printed) == 1
    listed = runner.invoke(cli_app, ["keys", "list"])
    assert listed.exit_code == 0
    assert secrets_printed[0] not in listed.stdout
    assert '"name": "Muse"' in listed.stdout
    with database.session() as session:
        key_id = session.scalars(select(ApiKey.id)).one()
    revoked = runner.invoke(cli_app, ["keys", "revoke", str(key_id)])
    assert revoked.exit_code == 0
    assert '"state": "revoked"' in revoked.stdout
    bad = runner.invoke(cli_app, ["keys", "create", "--name", "x", "--scope", "posts:delete"])
    assert bad.exit_code == 2
    assert set(ALL_SCOPES) >= DEFAULT_AGENT_SCOPES
