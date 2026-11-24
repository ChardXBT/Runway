"""Compliance surface: Meta signed_request, workspace export/purge, self-delete.

Spec 05 sections 4-5.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from urllib.parse import quote

import pytest

from runway.auth.problems import Problem
from runway.config import Settings
from runway.db.base import Database
from runway.db.bootstrap import initialize_database
from runway.db.models import (
    AuditLogEntry,
    ConnectedAccount,
    User,
    Workspace,
    WorkspaceMember,
)
from runway.realtime.meta_hooks import _signed_request_field, verify_signed_request
from runway.workspaces.lifecycle import (
    delete_user,
    export_workspace_data,
    purge_workspace,
)

SECRET = "meta-app-secret"
OTHER = "other-secret"


def _signed(payload: dict[str, object], secret: str = SECRET) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{base64.urlsafe_b64encode(sig).decode('ascii').rstrip('=')}.{body}"


def _database(tmp_path: Path) -> Database:
    return initialize_database(Settings(data_dir=tmp_path / "data"))


def _workspace(session, slug: str = "ws") -> Workspace:
    workspace = Workspace(slug=slug, name=slug)
    session.add(workspace)
    session.flush()
    return workspace


def _member(session, workspace: Workspace, email: str, role: str) -> tuple[User, WorkspaceMember]:
    user = User(external_id=email, email=email, display_name=email)
    session.add(user)
    session.flush()
    member = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=role)
    session.add(member)
    session.flush()
    return user, member


# ------------------------------------------------------------- signed_request


def test_signed_request_roundtrip() -> None:
    payload = {"user_id": "123", "algorithm": "HMAC-SHA256", "issued_at": 1}
    assert verify_signed_request(_signed(payload), [OTHER, SECRET]) == payload


def test_signed_request_rejects_wrong_secret_and_junk() -> None:
    payload = {"user_id": "1", "algorithm": "HMAC-SHA256"}
    assert verify_signed_request(_signed(payload, OTHER), [SECRET]) is None
    assert verify_signed_request("not.a.request", [SECRET]) is None
    assert verify_signed_request("nosplit", [SECRET]) is None
    # An unsigned or re-signed payload under an attacker key never verifies.
    forged = _signed(payload, "attacker")
    assert verify_signed_request(forged, [SECRET, OTHER]) is None


def test_signed_request_field_reads_form_body() -> None:
    value = _signed({"user_id": "9", "algorithm": "HMAC-SHA256"})
    body = f"signed_request={quote(value, safe='')}".encode()
    assert _signed_request_field(body) == value
    assert _signed_request_field(b"other=1") is None


# ------------------------------------------------------------------- export


def test_export_includes_every_workspace_table(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.session() as session:
        workspace = _workspace(session)
        other = _workspace(session, "other")
        _member(session, workspace, "a@x.test", "owner")
        session.add(
            ConnectedAccount(
                workspace_id=workspace.id,
                platform="tiktok",
                external_account_id="ext-1",
                status="connected",
            )
        )
        session.add(
            ConnectedAccount(
                workspace_id=other.id,
                platform="tiktok",
                external_account_id="ext-2",
                status="connected",
            )
        )
        session.flush()
        document = export_workspace_data(session, workspace.id)
    assert document["format_version"] == 1
    assert document["workspace"]["slug"] == "ws"
    accounts = document["tables"]["connected_accounts"]
    assert [row["external_account_id"] for row in accounts] == ["ext-1"]
    assert document["tables"]["users"][0]["email"] == "a@x.test"
    database.engine.dispose()


def test_purge_workspace_removes_every_row(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.session() as session:
        workspace = _workspace(session)
        _member(session, workspace, "b@x.test", "owner")
        session.add(
            ConnectedAccount(
                workspace_id=workspace.id,
                platform="tiktok",
                external_account_id="ext-1",
                status="disconnected",
            )
        )
        session.add(
            AuditLogEntry(
                workspace_id=workspace.id,
                actor_kind="user",
                actor_id="1",
                action="workspace.export",
                detail_json="{}",
            )
        )
        session.flush()
        purge_workspace(session, workspace.id)
        session.flush()
        assert session.get(Workspace, workspace.id) is None
        assert session.scalar(ConnectedAccount.__table__.select()) is None
        assert not session.scalars(AuditLogEntry.__table__.select()).all()
        # The person is not workspace data; their users row survives.
        assert session.scalars(User.__table__.select()).all()
    database.engine.dispose()


# -------------------------------------------------------------- self-delete


def test_delete_user_removes_memberships_and_row(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.session() as session:
        workspace = _workspace(session)
        user, _ = _member(session, workspace, "c@x.test", "viewer")
        _member(session, workspace, "d@x.test", "owner")
        uid = user.id
        workspace_ids = delete_user(session, uid)
        session.flush()
        assert workspace_ids == [workspace.id]
        assert session.get(User, uid) is None
        assert not session.scalars(
            WorkspaceMember.__table__.select().where(WorkspaceMember.user_id == uid)
        ).all()
    database.engine.dispose()


def test_delete_user_refuses_last_owner_with_members(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.session() as session:
        workspace = _workspace(session)
        owner, _ = _member(session, workspace, "e@x.test", "owner")
        _member(session, workspace, "f@x.test", "viewer")
        with pytest.raises(Problem):
            delete_user(session, owner.id)
    database.engine.dispose()


def test_delete_user_allows_last_owner_of_empty_workspace(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.session() as session:
        workspace = _workspace(session)
        owner, _ = _member(session, workspace, "g@x.test", "owner")
        assert delete_user(session, owner.id) == [workspace.id]
    database.engine.dispose()
