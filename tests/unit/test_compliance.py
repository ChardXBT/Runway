"""Compliance surface: Meta signed_request, workspace export/purge, self-delete.

Spec 05 sections 4-5.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from r2_support import Clock, FakeR2, fake_r2_store
from sqlalchemy import func, select

from runway.api.app import create_app
from runway.auth.problems import Problem
from runway.config import Settings
from runway.db.base import Database
from runway.db.bootstrap import initialize_database
from runway.db.models import (
    AccountToken,
    ApiKey,
    AuditLogEntry,
    CandidateImage,
    CaptionCandidateRecord,
    CaptionSlate,
    Channel,
    ConnectedAccount,
    GenerationRun,
    MediaAsset,
    MediaAssetV2,
    OAuthState,
    PlatformSubscription,
    Proposal,
    ProposalEvent,
    PublishJob,
    SearchRun,
    User,
    WebhookEndpoint,
    WebhookEvent,
    Workspace,
    WorkspaceMember,
)
from runway.jobs.queue import JobQueue
from runway.mediastore.keys import make_media_key
from runway.mediastore.local import LocalDiskStore
from runway.posts.media_service import poster_root
from runway.realtime.meta_hooks import (
    _signed_request_field,
    build_meta_hooks_router,
    verify_signed_request,
)
from runway.realtime.processing import _process_meta
from runway.workspaces.lifecycle import (
    DELETE_WORKSPACE_JOB,
    EXPORT_WORKSPACE_JOB,
    delete_user,
    export_key,
    export_path,
    export_workspace_data,
    purge_workspace,
    run_delete_workspace_job,
)

SECRET = "meta-app-secret"
OTHER = "other-secret"


def _signed(payload: dict[str, object], secret: str = SECRET) -> str:
    raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    body = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return f"{base64.urlsafe_b64encode(sig).decode('ascii').rstrip('=')}.{body}"


def _meta_event(session, provider: str, user_id: str) -> WebhookEvent:
    """A stored Meta compliance delivery whose signed_request verifies."""
    signed = _signed({"user_id": user_id, "algorithm": "HMAC-SHA256", "issued_at": 1})
    body = f"signed_request={quote(signed, safe='')}".encode()
    event = WebhookEvent(
        provider=provider,
        signature_ok=True,
        dedupe_key=f"{provider}:{hashlib.sha256(body).hexdigest()}",
        body=body,
    )
    session.add(event)
    session.flush()
    return event


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


def test_data_deletion_status_page_requires_a_hex_confirmation_code(
    tmp_path: Path,
) -> None:
    """The status page answers 404 to any code that is not 64 lowercase hex.

    The code is reflected into the HTML on a page reachable without a
    principal (Meta links users to it), so a path carrying markup must not
    come back in the body - and can never name a stored request anyway.
    """
    settings = Settings(data_dir=tmp_path / "data")
    database = initialize_database(settings)
    application = FastAPI()
    application.include_router(build_meta_hooks_router(database, settings))
    try:
        with TestClient(application, base_url="http://127.0.0.1:8000") as client:
            code = hashlib.sha256(b"signed-body").hexdigest()
            unknown = client.get(f"/hooks/meta/data-deletion/{code}")
            assert unknown.status_code == 404
            with database.session() as session:
                session.add(
                    WebhookEvent(
                        provider="meta-data-deletion",
                        signature_ok=True,
                        dedupe_key=f"meta-data-deletion:{code}",
                        body=b"signed-body",
                    )
                )
            page = client.get(f"/hooks/meta/data-deletion/{code}")
            assert page.status_code == 200
            assert code in page.text
            for bad in (
                "<script>alert(1)</script>",
                code[:-1],  # 63 chars
                code.upper(),  # uppercase hex is not the issued form
                "x" * 64,  # right length, not hex
            ):
                response = client.get(f"/hooks/meta/data-deletion/{bad}")
                assert response.status_code == 404, (bad, response.status_code)
                assert bad not in response.text
    finally:
        database.engine.dispose()


def test_meta_deletion_matches_ig_id_or_the_app_scoped_meta_user_id(
    tmp_path: Path,
) -> None:
    """Unified Login accounts are found by `meta_user_id`, direct ones by IG id.

    Meta's signed_request names the app-scoped Meta user id: an Instagram
    Login account's external id is the IG user id, while a Unified Login
    account records `meta_user_id` in metadata_json at connect. A deletion
    request must reach the account through either field.
    """
    database = _database(tmp_path)
    settings = Settings(data_dir=tmp_path / "data", meta_app_secret=SECRET)
    queue = JobQueue(database)
    try:
        with database.session() as session:
            direct = ConnectedAccount(
                workspace_id=1,
                platform="instagram",
                external_account_id="ig-direct-1",
                status="connected",
                metadata_json="{}",
            )
            unified = ConnectedAccount(
                workspace_id=1,
                platform="instagram",
                external_account_id="ig-unified-1",
                status="connected",
                metadata_json=json.dumps(
                    {"connection": "instagram_unified", "meta_user_id": "meta-7"}
                ),
            )
            session.add_all([direct, unified])
            session.flush()
            for user_id, account in (("meta-7", unified), ("ig-direct-1", direct)):
                event = _meta_event(session, "meta-data-deletion", user_id)
                _process_meta(session, database, settings, queue, event)
                session.flush()
                assert event.account_id == account.id
                assert event.error is None
                assert account.status == "disconnected"
                assert account.external_account_id.startswith("purged:")
                assert account.metadata_json == "{}"
            # A Meta user we never connected is acknowledged, not an error.
            unknown = _meta_event(session, "meta-data-deletion", "meta-nobody")
            _process_meta(session, database, settings, queue, unknown)
            assert unknown.account_id is None
            assert unknown.error is None
            assert unknown.processed_at is not None

            # The deauthorize path disconnects without the column scrub.
            revived = ConnectedAccount(
                workspace_id=1,
                platform="instagram",
                external_account_id="ig-revived",
                status="connected",
                metadata_json="{}",
            )
            session.add(revived)
            session.flush()
            deauth = _meta_event(session, "meta-deauthorize", "ig-revived")
            _process_meta(session, database, settings, queue, deauth)
            session.flush()
            assert deauth.account_id == revived.id
            assert revived.status == "disconnected"
            assert revived.external_account_id == "ig-revived"

            # Every Meta-initiated detach is audited: no caller principal
            # exists on a compliance callback, so the platform is the actor.
            rows = session.scalars(
                select(AuditLogEntry).where(AuditLogEntry.action == "account.disconnect")
            ).all()
            assert len(rows) == 3
            assert all(row.actor_kind == "platform" for row in rows)
            assert all(row.actor_id == "instagram" for row in rows)
            assert {row.target_id for row in rows} == {
                str(direct.id),
                str(unified.id),
                str(revived.id),
            }
            assert all(json.loads(row.detail_json)["platform"] == "instagram" for row in rows)
    finally:
        database.engine.dispose()


# ------------------------------------------------------------------- export


def test_export_includes_every_workspace_table(tmp_path: Path) -> None:
    database = _database(tmp_path)
    # Two workspaces are seeded: fixture writes are system work, not bound
    # to one workspace's session.
    with database.system_session() as session:
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


def test_export_carries_no_secret_column_values(tmp_path: Path) -> None:
    """The per-table dump applies a denylist to every table, not just tokens.

    A GDPR export must never hand out grant ciphertext, signing secrets,
    callback tokens or key hashes - the columns below each hold a sentinel
    that must not appear anywhere in the document.
    """
    database = _database(tmp_path)
    sentinels = {
        "token_ref": "s3ntinel-token-ref",
        "ciphertext": "s3ntinel-grant-bytes",
        "nonce": "s3ntinel-nonce-bytes",
        "code_verifier": "s3ntinel-verifier",
        "token_ciphertext": "s3ntinel-parked",
        "secret_encrypted": "s3ntinel-hook-secret",
        "key_hash": "s3ntinel-key-hash",
        "hub_secret": "s3ntinel-hub",
        "callback_token": "s3ntinel-callback",
    }
    # `_workspace` creates a non-default workspace; seeding its rows is
    # system work, not a bound workspace session.
    with database.system_session() as session:
        workspace = _workspace(session)
        _member(session, workspace, "exp@x.test", "owner")
        account = ConnectedAccount(
            workspace_id=workspace.id,
            platform="instagram",
            external_account_id="ext-9",
            status="connected",
            token_ref=sentinels["token_ref"],
        )
        session.add(account)
        session.flush()
        session.add(
            AccountToken(
                workspace_id=workspace.id,
                account_id=account.id,
                key_version="v1",
                ciphertext=sentinels["ciphertext"].encode("utf-8"),
                nonce=sentinels["nonce"].encode("utf-8"),
            )
        )
        session.add(
            OAuthState(
                workspace_id=workspace.id,
                platform="instagram",
                state="state-1",
                code_verifier=sentinels["code_verifier"],
                token_ciphertext=sentinels["token_ciphertext"],
                redirect_uri="https://edge.test/cb",
                expires_at=datetime.now(UTC) + timedelta(minutes=5),
            )
        )
        session.add(
            WebhookEndpoint(
                workspace_id=workspace.id,
                url="https://hooks.test/endpoint",
                secret_encrypted=sentinels["secret_encrypted"],
            )
        )
        session.add(
            ApiKey(
                workspace_id=workspace.id,
                name="agent",
                prefix="rwy_abcd",
                key_hash=sentinels["key_hash"],
            )
        )
        session.add(
            PlatformSubscription(
                workspace_id=workspace.id,
                account_id=account.id,
                provider="youtube",
                topic="topic-1",
                hub_secret=sentinels["hub_secret"],
                callback_token=sentinels["callback_token"],
            )
        )
        session.flush()
        document = export_workspace_data(session, workspace.id)
    rendered = json.dumps(document, default=str)
    for column, value in sentinels.items():
        assert value not in rendered, column
        # Byte columns serialize as base64 when they leak.
        assert base64.b64encode(value.encode("utf-8")).decode("ascii") not in rendered
    denied = {
        "token_ref",
        "ciphertext",
        "nonce",
        "code_verifier",
        "token_ciphertext",
        "secret_encrypted",
        "key_hash",
        "hub_secret",
        "callback_token",
    }
    for name in (
        "account_tokens",
        "oauth_states",
        "webhook_endpoints",
        "api_keys",
        "connected_accounts",
        "platform_subscriptions",
    ):
        rows = document["tables"][name]
        assert rows, name  # the fixture wrote a row; an empty table proves nothing
        for row in rows:
            assert not (denied & row.keys()), name
    database.engine.dispose()


def test_delete_workspace_job_removes_the_workspaces_stored_objects(tmp_path: Path) -> None:
    """The purge job deletes media objects, posters and export artifacts.

    Keys are read from the rows before the purge, so every store the
    workspace touched - local disk and R2 alike - is asked to delete them.
    The other workspace's objects under the same stores stay untouched.
    """
    database = _database(tmp_path)
    settings = Settings(data_dir=tmp_path / "data")
    clock = Clock()
    fake = FakeR2(clock)
    local = LocalDiskStore.from_settings(settings, part_size=1024)
    r2 = fake_r2_store(fake, clock)
    stores: dict[str, object] = {"local": local, "r2": r2}
    digest = hashlib.sha256(b"media").hexdigest()

    # Seeding rows across two workspaces is system work, not one
    # workspace's bound session.
    with database.system_session() as session:
        victim = _workspace(session, "victim")
        assets = []
        for workspace_id, storage, name in (
            (1, "local", "keep-local"),
            (victim.id, "r2", "gone-r2"),
            (victim.id, "local", "gone-local"),
        ):
            key = make_media_key(workspace_id, name, digest, "mp4")
            store = stores[storage]
            store.put_object(
                key, b"media-bytes", workspace_id=workspace_id, content_type="video/mp4"
            )
            asset = MediaAssetV2(
                workspace_id=workspace_id,
                content_type="video/mp4",
                size_bytes=11,
                sha256=digest,
                storage=storage,
                storage_key=key,
                status="ready",
            )
            session.add(asset)
            session.flush()
            assets.append(asset)
        keep_asset, gone_r2, gone_local = assets
        # Workspace 1's local poster file must survive; the victim's must go.
        keep_poster = poster_root(settings) / "w1" / f"{keep_asset.id}.jpg"
        keep_poster.parent.mkdir(parents=True, exist_ok=True)
        keep_poster.write_bytes(b"poster")
        keep_asset.poster_key = f"poster:{keep_asset.id}"
        gone_poster_key = make_media_key(victim.id, f"{gone_r2.id}-poster", digest, "jpg")
        r2.put_object(gone_poster_key, b"poster", workspace_id=victim.id, content_type="image/jpeg")
        gone_r2.poster_key = gone_poster_key
        local_poster = poster_root(settings) / f"w{victim.id}" / f"{gone_local.id}.jpg"
        local_poster.parent.mkdir(parents=True, exist_ok=True)
        local_poster.write_bytes(b"poster")
        gone_local.poster_key = f"poster:{gone_local.id}"
        # Export artifacts: the victim's r2 object and local file are removed;
        # workspace 1's export object must survive.
        gone_export = "aaaaaaaabbbbcccc"
        r2.put_object(
            export_key(victim.id, gone_export),
            b"export",
            workspace_id=victim.id,
            content_type="application/json",
        )
        keep_export = "1111111122222222"
        r2.put_object(
            export_key(1, keep_export),
            b"export",
            workspace_id=1,
            content_type="application/json",
        )
        local_export = export_path(settings, victim.id, "ddddddddeeeeeeee")
        local_export.parent.mkdir(parents=True, exist_ok=True)
        local_export.write_bytes(b"export")
        for workspace_id, export_id in ((victim.id, gone_export), (1, keep_export)):
            session.add(
                PublishJob(
                    workspace_id=workspace_id,
                    kind=EXPORT_WORKSPACE_JOB,
                    state="finished",
                    idempotency_key=f"export:{export_id}",
                    payload_json=json.dumps({"workspace_id": workspace_id, "export_id": export_id}),
                )
            )
        session.flush()
        victim_id = victim.id

    job = SimpleNamespace(payload={"workspace_id": victim_id})
    run_delete_workspace_job(database, settings, job, stores=stores)

    assert (local.root / make_media_key(1, "keep-local", digest, "mp4")).is_file()
    assert keep_poster.is_file()
    assert export_key(1, keep_export) in fake.objects
    for key in (
        make_media_key(victim_id, "gone-r2", digest, "mp4"),
        gone_poster_key,
        export_key(victim_id, gone_export),
    ):
        assert key not in fake.objects
    assert not (local.root / make_media_key(victim_id, "gone-local", digest, "mp4")).exists()
    assert not local_poster.exists()
    assert not local_export.exists()
    with database.session() as session:
        assert session.get(Workspace, victim_id) is None
        assert session.get(Workspace, 1) is not None
    database.engine.dispose()


def test_workspace_delete_route_refuses_the_local_profiles_workspace(
    tmp_path: Path,
) -> None:
    """DELETE /api/v1/workspace in `local`: a 422 without the echo, then a 409.

    Workspace 1 is the local profile's only workspace - deleting it would
    leave the install with no workspace at all, so even the correct echo is
    refused with `invalid_state` and nothing is scheduled.
    """
    settings = Settings(data_dir=tmp_path / "data")
    database = initialize_database(settings)
    try:
        with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
            view = client.get("/api/v1/workspace")
            assert view.status_code == 200, view.text
            slug = str(view.json()["slug"])
            for kwargs in (
                {},
                {"json": {"confirm": "not-the-workspace"}},
                {"json": {"confirm": ""}},
            ):
                response = client.request("DELETE", "/api/v1/workspace", **kwargs)
                assert response.status_code == 422, response.text
                assert response.json()["code"] == "validation_failed"
            refused = client.request("DELETE", "/api/v1/workspace", json={"confirm": slug})
            assert refused.status_code == 409, refused.text
            assert refused.json()["code"] == "invalid_state"
        with database.session() as session:
            workspace = session.get(Workspace, 1)
            assert workspace is not None and workspace.deleted_at is None
            scheduled = session.scalar(
                select(func.count())
                .select_from(PublishJob)
                .where(PublishJob.kind == DELETE_WORKSPACE_JOB)
            )
            assert scheduled == 0
    finally:
        database.engine.dispose()


def test_download_export_refuses_a_malformed_export_id(tmp_path: Path) -> None:
    r"""An id outside the generated hex form is a 404 before any path is built.

    The export id becomes a file path on `local` and an object key on
    `hosted`; anything that is not the issued 16-lowercase-hex form can name
    neither, so it must never reach either store. On Windows a backslash in
    the id is a path separator - a `..\` segment climbs out of the workspace's
    export directory - which the hex gate rejects before `export_path` is built.
    """
    settings = Settings(data_dir=tmp_path / "data")
    database = initialize_database(settings)
    # A file one directory up from ws1's exports: reachable as `..\escape`
    # while the route builds paths by plain join.
    escaped = settings.resolved_data_dir / "exports" / "escape.json"
    escaped.parent.mkdir(parents=True, exist_ok=True)
    escaped.write_text("{}", encoding="utf-8")
    try:
        with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as client:
            for bad in (
                "not-hex",
                "A" * 16,
                "1" * 15,
                "0" * 17,
                "..%5Cescape",  # Windows traversal out of ws1's export dir
                "escape%5C..%5C..%5Cescape",
            ):
                response = client.get(f"/api/v1/workspace/exports/{bad}")
                assert response.status_code == 404, (bad, response.status_code, response.text)
            # A well-formed but never-issued id is still a plain 404.
            missing = client.get("/api/v1/workspace/exports/" + "0" * 16)
            assert missing.status_code == 404
    finally:
        database.engine.dispose()


def test_purge_workspace_removes_every_row(tmp_path: Path) -> None:
    database = _database(tmp_path)
    # Purge is system work across every workspace's rows.
    with database.system_session() as session:
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


def _channel_graph(session, workspace: Workspace) -> Channel:
    """The channel-scoped pipeline subgraph no workspace-owned row names directly."""
    channel = Channel(
        workspace_id=workspace.id,
        name="ch",
        handle=f"@{workspace.slug}",
        timezone="UTC",
        default_post_time="09:00",
        planning_horizon_days=7,
        duplicate_window_days=30,
    )
    session.add(channel)
    session.flush()
    run = GenerationRun(channel_id=channel.id, start_date=date(2025, 1, 1), days=7, status="done")
    search = SearchRun(
        channel_id=channel.id,
        query_plan_json="{}",
        provider="manual",
        status="done",
    )
    asset = MediaAsset(
        kind="image",
        local_path="/tmp/x.jpg",
        mime_type="image/jpeg",
        width=4,
        height=4,
        file_size=10,
        sha256=f"{workspace.id:064d}",
        perceptual_hash="p" * 64,
    )
    session.add_all([run, search, asset])
    session.flush()
    candidate = CandidateImage(
        search_run_id=search.id,
        media_asset_id=asset.id,
        search_query="q",
        result_rank=1,
        quality_score=1.0,
        style_score=1.0,
        novelty_score=1.0,
        caption_potential_score=1.0,
        source_risk_score=1.0,
        final_rank_score=1.0,
    )
    session.add(candidate)
    session.flush()
    proposal = Proposal(
        generation_run_id=run.id,
        channel_id=channel.id,
        candidate_image_id=candidate.id,
        planned_publish_at="2025-01-01T09:00",
        recommended_caption="c",
        alternative_captions_json="[]",
        final_caption="c",
        status="pending",
        selection_reason="r",
        style_score=1.0,
        novelty_score=1.0,
        quality_score=1.0,
    )
    session.add(proposal)
    session.flush()
    slate = CaptionSlate(
        channel_id=channel.id,
        candidate_image_id=candidate.id,
        proposal_id=proposal.id,
        editorial_brief_json="{}",
        raw_output_json="{}",
        generation_configuration_json="{}",
        configuration_hash=f"{workspace.id:064x}",
        status="done",
    )
    session.add(slate)
    session.flush()
    # Close the proposals <-> caption_slates cycle and hang children off both.
    proposal.caption_slate_id = slate.id
    session.add(ProposalEvent(proposal_id=proposal.id, event_type="created"))
    session.add(
        CaptionCandidateRecord(
            channel_id=channel.id,
            caption_slate_id=slate.id,
            text="cap",
            structure="hook-line",
            editorial_angle="angle",
            rank=1,
        )
    )
    session.flush()
    return channel


def test_purge_workspace_reaches_the_channel_subgraph_and_breaks_the_fk_cycle(
    tmp_path: Path,
) -> None:
    """A workspace holding proposals and caption_slates purges cleanly.

    The cycle tables carry no `workspace_id`; the purge must follow the
    channel foreign keys, and `proposals`/`caption_slates` reference each
    other through nullable columns no topological order can resolve.
    """
    database = _database(tmp_path)
    # Two workspaces are seeded and one purged: system work, not bound to
    # either workspace's session.
    with database.system_session() as session:
        workspace = _workspace(session)
        other = _workspace(session, "other")
        _member(session, workspace, "purge@x.test", "owner")
        channel = _channel_graph(session, workspace)
        channel_id = channel.id
        other_channel = _channel_graph(session, other)
        other_channel_id = other_channel.id
        purge_workspace(session, workspace.id)
        session.flush()
        # Every row of the deleted channel's subgraph is gone, whichever table
        # held it; the other workspace's identical graph survives untouched.
        for model in (
            GenerationRun,
            SearchRun,
            CandidateImage,
            Proposal,
            CaptionSlate,
            CaptionCandidateRecord,
        ):
            left = session.scalars(model.__table__.select()).all()
            assert len(left) == 1, model.__tablename__
        # The surviving proposal_event is the other workspace's (id 2).
        assert session.scalars(ProposalEvent.__table__.select()).all() == [2]
        assert not session.scalars(Channel.__table__.select().where(Channel.id == channel_id)).all()
        assert session.scalars(
            Channel.__table__.select().where(Channel.id == other_channel_id)
        ).all()
        # media_assets are content-addressed and never workspace-keyed.
        assert len(session.scalars(MediaAsset.__table__.select()).all()) == 2
        # Workspace 1 is the bootstrap workspace; `other` survives, `workspace` is gone.
        remaining = session.scalars(Workspace.__table__.select()).all()
        assert workspace.id not in remaining and other.id in remaining
        assert session.scalars(User.__table__.select()).all()
    database.engine.dispose()


# -------------------------------------------------------------- self-delete


def test_delete_user_removes_memberships_and_row(tmp_path: Path) -> None:
    database = _database(tmp_path)
    # delete_user spans the person's memberships in every workspace: it is
    # system work, not one workspace's bound session.
    with database.system_session() as session:
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
    with database.system_session() as session:
        workspace = _workspace(session)
        owner, _ = _member(session, workspace, "e@x.test", "owner")
        _member(session, workspace, "f@x.test", "viewer")
        with pytest.raises(Problem):
            delete_user(session, owner.id)
    database.engine.dispose()


def test_delete_user_allows_last_owner_of_empty_workspace(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.system_session() as session:
        workspace = _workspace(session)
        owner, _ = _member(session, workspace, "g@x.test", "owner")
        assert delete_user(session, owner.id) == [workspace.id]
    database.engine.dispose()
