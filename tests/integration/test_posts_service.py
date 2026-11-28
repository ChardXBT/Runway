"""PostService: validation, release bound to the payload hash, edits that clear a release."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from runway.auth.principal import ALL_SCOPES, Principal, local_owner
from runway.auth.problems import Problem
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ConnectedAccount, DraftTarget, EventRecord, PublishJob
from runway.platforms.drafts import DraftService
from runway.platforms.publisher_registry import build_publisher_registry
from runway.posts.schemas import (
    PostCreate,
    PostPatch,
    ReleaseRequest,
    ScheduleRequest,
    TargetInput,
    TargetUpdate,
)
from runway.posts.service import PostService

NOW = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
YOUTUBE = {
    "title": "Launch",
    "category_id": "22",
    "privacy": "unlisted",
    "made_for_kids": False,
    "has_paid_product_placement": False,
    "schedule_mode": "held",
}


class Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def agent(scopes: frozenset[str] = ALL_SCOPES) -> Principal:
    return Principal(
        kind="agent", id="key-1", workspace_id=1, role=None, scopes=scopes, display="Agent key"
    )


def add_account(database: Database, platform: str, *, workspace_id: int = 1) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            workspace_id=workspace_id,
            platform=platform,
            external_account_id=f"{platform}-ext",
            handle=f"@{platform}",
            status="connected",
            granted_scopes_json="[]",
            token_ref=None,
            metadata_json="{}",
        )
        session.add(account)
        session.flush()
        return account.id


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def service(database: Database, settings: Settings, clock: Clock) -> PostService:
    registry = build_publisher_registry(settings, database, clock=clock)
    return PostService(database, settings, registry, clock=clock)


def _youtube_post(service: PostService, account_id: int, **settings: object) -> tuple[int, int]:
    view = service.create_post(
        local_owner(),
        PostCreate(
            title="Launch",
            body="Caption text",
            targets=[
                TargetInput(
                    platform="youtube",  # type: ignore[arg-type]
                    account_id=account_id,
                    post_type="youtube_video",
                    settings={**YOUTUBE, **settings},
                    planned_at=NOW + timedelta(hours=2),
                )
            ],
        ),
    )
    return view.id, view.targets[0].id


def test_a_text_only_youtube_target_is_ready_when_settings_are_complete(
    database: Database, service: PostService
) -> None:
    account = add_account(database, "youtube")
    post_id, _ = _youtube_post(service, account)
    target = service.get_post(local_owner(), post_id).targets[0]
    # A YouTube video needs its video file; nothing else is missing.
    assert {issue.code for issue in target.issues if issue.severity == "error"} == {"media_count"}
    assert target.state == "draft"
    assert target.publisher == "assisted"


def _ready_post(
    database: Database, service: PostService, clock: Clock, **settings: object
) -> tuple[int, int]:
    """A YouTube Community post (no media required) that validates cleanly."""
    account = add_account(database, "youtube")
    view = service.create_post(
        local_owner(),
        PostCreate(
            title="Note",
            body="Hello community",
            targets=[
                TargetInput(
                    platform="youtube",  # type: ignore[arg-type]
                    account_id=account,
                    post_type="youtube_post",
                    settings=dict(settings),
                    planned_at=clock.now + timedelta(hours=1),
                )
            ],
        ),
    )
    return view.id, view.targets[0].id


def test_release_needs_the_echoed_hash_and_confirmation(
    database: Database, service: PostService, clock: Clock
) -> None:
    post_id, target_id = _ready_post(database, service, clock)
    target = service.get_post(local_owner(), post_id).targets[0]
    assert target.state == "ready" and target.releasable

    with pytest.raises(Problem) as raised:
        service.release_target(
            local_owner(), post_id, target_id, ReleaseRequest(payload_hash="0" * 64, confirmed=True)
        )
    assert raised.value.code == "version_conflict"
    assert raised.value.extra["current_payload_hash"] == target.payload_hash
    with pytest.raises(Problem) as raised:
        service.release_target(
            local_owner(),
            post_id,
            target_id,
            ReleaseRequest(payload_hash=target.payload_hash, confirmed=False),
        )
    assert raised.value.code == "validation_failed"

    view = service.release_target(
        local_owner(),
        post_id,
        target_id,
        ReleaseRequest(payload_hash=target.payload_hash, confirmed=True),
    )
    released = view.targets[0]
    assert released.state == "queued"
    assert released.release.state == "released"
    assert released.release.payload_hash == target.payload_hash
    assert released.release.released_by == "user:local-owner"
    with database.session() as session:
        jobs = session.scalars(select(PublishJob).where(PublishJob.target_id == target_id)).all()
        assert [(job.kind, job.idempotency_key) for job in jobs] == [
            ("send", f"{target_id}:{target.payload_hash}")
        ]
        assert json.loads(jobs[0].payload_json)["payload_hash"] == target.payload_hash
        # Held until planned_at (assisted produces its checklist at the planned time).
        assert jobs[0].run_at.replace(tzinfo=UTC) == clock.now + timedelta(hours=1)
    # A second release of the same target is refused (it is queued, not ready).
    with pytest.raises(Problem) as raised:
        service.release_target(
            local_owner(),
            post_id,
            target_id,
            ReleaseRequest(payload_hash=target.payload_hash, confirmed=True),
        )
    assert raised.value.code == "invalid_state"


def test_editing_a_released_target_clears_the_release_and_cancels_its_job(
    database: Database, service: PostService, clock: Clock
) -> None:
    post_id, target_id = _ready_post(database, service, clock)
    target = service.get_post(local_owner(), post_id).targets[0]
    service.release_target(
        local_owner(),
        post_id,
        target_id,
        ReleaseRequest(payload_hash=target.payload_hash, confirmed=True),
    )
    post = service.get_post(local_owner(), post_id)

    edited = service.update_post(local_owner(), post_id, post.version, PostPatch(body="Changed"))

    after = edited.targets[0]
    assert after.state == "ready"
    assert after.release.state == "none" and after.release.payload_hash is None
    assert after.payload_hash != target.payload_hash
    assert after.state_reason == "edited after release; release it again"
    with database.session() as session:
        job = session.scalar(select(PublishJob).where(PublishJob.target_id == target_id))
        assert job is not None and job.state == "cancelled"
        changes = [
            json.loads(event.payload_json)
            for event in session.scalars(
                select(EventRecord).where(EventRecord.type == "target.state_changed")
            )
        ]
    assert [(change["from"], change["to"]) for change in changes] == [
        ("draft", "ready"),
        ("ready", "released"),
        ("released", "queued"),
        ("queued", "ready"),
    ]


def test_rescheduling_and_target_edits_also_clear_the_release(
    database: Database, service: PostService, clock: Clock
) -> None:
    post_id, target_id = _ready_post(database, service, clock)
    for change in ("schedule", "target"):
        target = service.get_post(local_owner(), post_id).targets[0]
        service.release_target(
            local_owner(),
            post_id,
            target_id,
            ReleaseRequest(payload_hash=target.payload_hash, confirmed=True),
        )
        queued = service.get_post(local_owner(), post_id).targets[0]
        if change == "schedule":
            view = service.schedule_target(
                local_owner(),
                post_id,
                target_id,
                ScheduleRequest(planned_at=clock.now + timedelta(hours=3)),
            )
        else:
            view = service.update_target(
                local_owner(),
                post_id,
                target_id,
                queued.version,
                TargetUpdate(post_type="youtube_post", caption_override="Different"),
            )
        assert view.targets[0].state == "ready", change
        assert view.targets[0].release.state == "none", change


def test_stale_if_match_changes_nothing(
    database: Database, service: PostService, clock: Clock
) -> None:
    post_id, _ = _ready_post(database, service, clock)
    post = service.get_post(local_owner(), post_id)
    service.update_post(local_owner(), post_id, post.version, PostPatch(title="New"))
    with pytest.raises(Problem) as raised:
        service.update_post(local_owner(), post_id, post.version, PostPatch(body="lost update"))
    assert raised.value.code == "version_conflict"
    assert service.get_post(local_owner(), post_id).body == "Hello community"


def test_a_target_without_explicit_privacy_cannot_be_released(
    database: Database, service: PostService, clock: Clock
) -> None:
    account = add_account(database, "tiktok")
    view = service.create_post(
        local_owner(),
        PostCreate(
            title="Dance",
            targets=[
                TargetInput(
                    platform="tiktok",  # type: ignore[arg-type]
                    account_id=account,
                    post_type="tiktok_video",
                    settings={"allow_comment": True},
                    planned_at=clock.now + timedelta(hours=1),
                )
            ],
        ),
    )
    target = view.targets[0]
    assert "privacy_required" in {issue.code for issue in target.issues}
    assert target.state == "draft" and not target.releasable
    with pytest.raises(Problem) as raised:
        service.release_target(
            local_owner(),
            view.id,
            target.id,
            ReleaseRequest(payload_hash=target.payload_hash, confirmed=True),
        )
    assert raised.value.status_code in {409, 422}


def test_youtube_target_without_privacy_cannot_be_released_even_if_forced_ready(
    database: Database, service: PostService, clock: Clock
) -> None:
    account = add_account(database, "youtube")
    post_id, target_id = _youtube_post(service, account)
    settings = {key: value for key, value in YOUTUBE.items() if key != "privacy"}
    with database.session() as session:
        target = session.get(DraftTarget, target_id)
        assert target is not None
        target.settings_json = json.dumps(settings)
        # Simulate a row that skipped validation.
        target.state = target.status = "ready"
    view = service.get_post(local_owner(), post_id).targets[0]
    with pytest.raises(Problem) as raised:
        service.release_target(
            local_owner(),
            post_id,
            target_id,
            ReleaseRequest(payload_hash=view.payload_hash, confirmed=True),
        )
    assert raised.value.code == "validation_failed"


def test_tiktok_needs_a_person_to_release_and_to_choose_settings(
    database: Database, service: PostService, clock: Clock
) -> None:
    account = add_account(database, "tiktok")
    view = service.create_post(
        agent(),
        PostCreate(
            title="Agent draft",
            targets=[
                TargetInput(
                    platform="tiktok",  # type: ignore[arg-type]
                    account_id=account,
                    post_type="tiktok_video",
                    planned_at=clock.now + timedelta(hours=1),
                )
            ],
        ),
    )
    target = view.targets[0]
    assert view.source == "agent" and view.ai_generated is True
    with pytest.raises(Problem) as raised:
        service.update_target(
            agent(),
            view.id,
            target.id,
            target.version,
            TargetUpdate(post_type="tiktok_video", settings={"privacy_level": "SELF_ONLY"}),
        )
    assert raised.value.code == "human_required"
    edited = service.update_target(
        local_owner(),
        view.id,
        target.id,
        target.version,
        TargetUpdate(post_type="tiktok_video", settings={"privacy_level": "SELF_ONLY"}),
    ).targets[0]
    for principal, batch in ((agent(), False), (local_owner(), True)):
        with pytest.raises(Problem) as raised:
            service.release_target(
                principal,
                view.id,
                target.id,
                ReleaseRequest(payload_hash=edited.payload_hash, confirmed=True),
                batch=batch,
            )
        assert raised.value.code == "human_required"
        assert raised.value.status_code == 403


def test_release_needs_the_release_scope(
    database: Database, service: PostService, clock: Clock
) -> None:
    post_id, target_id = _ready_post(database, service, clock)
    target = service.get_post(local_owner(), post_id).targets[0]
    proposer = agent(frozenset({"posts:read", "posts:write", "posts:schedule"}))
    with pytest.raises(Problem) as raised:
        service.release_target(
            proposer,
            post_id,
            target_id,
            ReleaseRequest(payload_hash=target.payload_hash, confirmed=True),
        )
    assert raised.value.code == "scope_missing"


def test_an_agent_created_target_that_becomes_ready_raises_awaiting_release(
    database: Database, service: PostService, clock: Clock
) -> None:
    account = add_account(database, "youtube")
    view = service.create_post(
        agent(),
        PostCreate(
            title="Agent note",
            body="From the agent",
            targets=[
                TargetInput(
                    platform="youtube",  # type: ignore[arg-type]
                    account_id=account,
                    post_type="youtube_post",
                    planned_at=clock.now + timedelta(hours=1),
                )
            ],
        ),
    )
    assert view.targets[0].state == "ready"
    with database.session() as session:
        types = [
            event.type for event in session.scalars(select(EventRecord).order_by(EventRecord.id))
        ]
    assert types == ["target.state_changed", "target.awaiting_release"]


def test_posts_are_workspace_scoped(database: Database, service: PostService, clock: Clock) -> None:
    post_id, _ = _ready_post(database, service, clock)
    with database.session() as session:
        from runway.db.models import Workspace

        session.add(Workspace(id=2, slug="other", name="Other"))
    stranger = Principal(
        kind="user", id="u2", workspace_id=2, role="owner", scopes=ALL_SCOPES, display="Other"
    )
    with pytest.raises(Problem) as raised:
        service.get_post(stranger, post_id)
    assert raised.value.code == "not_found"


def test_cancel_only_before_sending(database: Database, service: PostService, clock: Clock) -> None:
    post_id, target_id = _ready_post(database, service, clock)
    target = service.get_post(local_owner(), post_id).targets[0]
    service.release_target(
        local_owner(),
        post_id,
        target_id,
        ReleaseRequest(payload_hash=target.payload_hash, confirmed=True),
    )
    view = service.cancel_target(local_owner(), post_id, target_id)
    assert view.targets[0].state == "cancelled"
    assert view.status == "cancelled"
    with database.session() as session:
        assert session.scalar(select(PublishJob.state)) == "cancelled"
        target_row = session.get(DraftTarget, target_id)
        assert target_row is not None
        target_row.state = target_row.status = "sending"
    with pytest.raises(Problem) as raised:
        service.cancel_target(local_owner(), post_id, target_id)
    assert raised.value.code == "invalid_state"


def test_legacy_drafts_routes_keep_working_on_the_new_columns(
    database: Database, settings: Settings
) -> None:
    drafts = DraftService(database, settings)
    view = drafts.create_draft(title="Legacy", targets=[{"platform": "youtube"}])
    target = view["targets"][0]  # type: ignore[index]
    with database.session() as session:
        row = session.get(DraftTarget, int(target["id"]))
        assert row is not None
        assert (row.state, row.status, row.post_type) == ("pending", "pending", "youtube_video")
    published = drafts.mark_published(
        int(view["id"]), int(target["id"]), "https://youtu.be/abcdefg"
    )
    assert published["targets"][0]["status"] == "published"  # type: ignore[index]
    with database.session() as session:
        row = session.get(DraftTarget, int(target["id"]))
        assert row is not None and row.state == "published"
