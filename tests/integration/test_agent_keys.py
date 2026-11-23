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
from runway.db.models import ApiKey, AuditLogEntry

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
    assert not {"posts:release", "inbox:reply", "accounts:manage"} & set(key["scopes"])


def test_a_key_never_receives_a_scope_its_creator_lacks(client: TestClient) -> None:
    admin = str(_create(client, scopes=["workspace:admin", "posts:read"])["secret"])
    refused = client.post(
        "/api/v1/keys",
        json={"name": "wider", "scopes": ["posts:read", "posts:write"]},
        headers=_bearer(admin),
    )
    assert refused.status_code == 403
    assert refused.json()["code"] == "scope_missing"
    allowed = client.post(
        "/api/v1/keys", json={"name": "narrower", "scopes": ["posts:read"]}, headers=_bearer(admin)
    )
    assert allowed.status_code == 201


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
    created = _create(client, scopes=["posts:read", "workspace:admin"])
    secret = str(created["secret"])
    response = client.get("/api/v1/keys", headers=_bearer(secret))
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
    created = _create(client, scopes=["workspace:admin"])
    secret = str(created["secret"])
    key = created["key"]
    assert isinstance(key, dict)
    assert client.get("/api/v1/keys", headers=_bearer(secret)).status_code == 200
    revoked = client.delete(f"/api/v1/keys/{key['id']}")
    assert revoked.status_code == 200
    assert revoked.json()["state"] == "revoked"
    assert client.get("/api/v1/keys", headers=_bearer(secret)).status_code == 401

    other = _create(client, scopes=["workspace:admin"])
    other_key = other["key"]
    assert isinstance(other_key, dict)
    with database.session() as session:
        row = session.get(ApiKey, other_key["id"])
        assert row is not None
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    expired = client.get("/api/v1/keys", headers=_bearer(str(other["secret"])))
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
        secret = str(_create(client, scopes=["workspace:admin"])["secret"])
        assert client.get("/api/v1/keys", headers=_bearer(secret)).status_code == 200
        assert client.get("/api/v1/keys", headers=_bearer(secret)).status_code == 200
        third = client.get("/api/v1/keys", headers=_bearer(secret))
        assert third.status_code == 429
        assert third.json()["code"] == "rate_limited"
        assert int(third.headers["retry-after"]) >= 1
        # Other principals are not affected.
        assert client.get("/api/v1/keys").status_code == 200


# --------------------------------------------------------------------- audit


def test_every_mutation_by_a_key_writes_an_audit_row_with_key_id_and_name(
    client: TestClient, database: Database
) -> None:
    admin = _create(client, name="Muse admin", scopes=["workspace:admin", "posts:read"])
    admin_key = admin["key"]
    assert isinstance(admin_key, dict)
    victim = _create(client, name="old")["key"]
    assert isinstance(victim, dict)
    before = len(_audit_rows(database))
    response = client.delete(f"/api/v1/keys/{victim['id']}", headers=_bearer(str(admin["secret"])))
    assert response.status_code == 200
    rows = _audit_rows(database)[before:]
    http_rows = [row for row in rows if row.action == "DELETE /api/v1/keys/{key_id}"]
    assert len(http_rows) == 1
    detail = json.loads(http_rows[0].detail_json)
    assert http_rows[0].actor_kind == "agent"
    assert http_rows[0].actor_id == str(admin_key["id"])
    assert detail["key_id"] == str(admin_key["id"])
    assert detail["key_name"] == "Muse admin"
    service_rows = [row for row in rows if row.action == "api_key.revoke"]
    assert service_rows and json.loads(service_rows[0].detail_json)["key_name"] == "Muse admin"
    # A failed mutation and a read write nothing.
    count = len(_audit_rows(database))
    client.delete("/api/v1/keys/99999", headers=_bearer(str(admin["secret"])))
    client.get("/api/v1/keys", headers=_bearer(str(admin["secret"])))
    assert len(_audit_rows(database)) == count


# -------------------------------------------------------------------- policy


def test_policy_and_release_grants_are_for_people_only(client: TestClient) -> None:
    admin = str(_create(client, scopes=["workspace:admin", "posts:read"])["secret"])
    response = client.put(
        "/api/v1/policy", json={"agent_release": "per_key"}, headers=_bearer(admin)
    )
    assert response.status_code == 403
    assert response.json()["code"] == "human_required"
    key = _create(client)["key"]
    assert isinstance(key, dict)
    grants = client.put(
        f"/api/v1/keys/{key['id']}/release-grants",
        json={"grants": [{"platform": "youtube"}]},
        headers=_bearer(admin),
    )
    assert grants.status_code == 403
    assert grants.json()["code"] == "human_required"


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


def test_the_daily_post_cap_counts_a_keys_created_posts(
    settings: Settings, database: Database
) -> None:
    service = ApiKeyService(database, settings)
    created = service.create(local_owner(), name="capped", scopes=["posts:write"])
    principal = service.authenticate(created.secret)
    from runway.auth.audit import record_audit

    with database.session() as session:
        enforce_daily_post_cap(session, principal, 2)
        for _ in range(2):
            record_audit(session, principal, "mcp:create_post", target_type="post", target_id=1)
    with database.session() as session, pytest.raises(Problem) as raised:
        enforce_daily_post_cap(session, principal, 2)
    assert raised.value.code == "rate_limited"
    # People are never capped.
    with database.session() as session:
        enforce_daily_post_cap(session, local_owner(), 0)


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
