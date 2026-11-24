"""TikTok release rules: the consent text is echoed in the release body.

The mandatory posting screen ends with an express consent; the release request
carries the exact string the human saw, and it is checked against the settings'
`release_notice` (spec 08 section 4, research/tiktok section 7).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select

from runway.auth.principal import local_owner
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    ConnectedAccount,
    DraftMedia,
    DraftTarget,
    MediaAssetV2,
    PublishJob,
)
from runway.platforms.capabilities import Platform
from runway.platforms.publisher import (
    Issue,
    PollResult,
    Progress,
    PublishContext,
    Publisher,
    ReconcileResult,
    SendResult,
    VerifyResult,
)
from runway.platforms.publisher_registry import PublisherRegistry
from runway.posts.schemas import PostCreate, ReleaseRequest, TargetInput
from runway.posts.service import PostService

NOW = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
CONSENT = "By posting, you agree to TikTok's Music Usage Confirmation"
DRAFT_NOTICE = "This sends a draft to your TikTok inbox. You finish and post it in the app."


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@dataclass
class FakeTikTokPublisher:
    """Stands in for the real adapters: a clean preflight and a processing send."""

    name: str
    supported: frozenset[str]
    kind: str = "direct"
    platform: Platform = Platform.tiktok
    sends: int = 0

    def supports(self, post_type: str) -> bool:
        return post_type in self.supported

    def preflight(self, ctx: PublishContext) -> list[Issue]:
        return []

    def send(self, ctx: PublishContext, progress: Progress) -> SendResult:
        self.sends += 1
        ctx.mark_request_sent()
        return SendResult(status="processing", external_id="pub-1")

    def poll(self, ctx: PublishContext) -> PollResult:
        return PollResult(status="processing")

    def verify(self, ctx: PublishContext) -> VerifyResult:
        return VerifyResult(verified=None)

    def reconcile(self, ctx: PublishContext) -> ReconcileResult:
        return ReconcileResult(status="absent", checked=("the fake platform",))


@pytest.fixture()
def clock() -> Clock:
    return Clock()


def _service(
    database: Database, settings: Settings, clock: Clock, *publishers: Publisher
) -> PostService:
    registry = PublisherRegistry()
    for publisher in publishers:
        registry.register(publisher)
    return PostService(database, settings, registry, clock=clock)


def _account(database: Database, *, scopes: list[str]) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform="tiktok",
            external_account_id="open-1",
            handle="@owner",
            status="connected",
            granted_scopes_json=json.dumps(scopes),
            token_ref=None,
            metadata_json="{}",
        )
        session.add(account)
        session.flush()
        return account.id


def _video_asset(database: Database, workspace_id: int = 1) -> int:
    data = b"fake video bytes"
    with database.session() as session:
        asset = MediaAssetV2(
            workspace_id=workspace_id,
            kind="video",
            content_type="video/mp4",
            size_bytes=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            storage="local",
            storage_key="w/1/videos/clip.mp4",
            status="ready",
            probe_json="{}",
        )
        session.add(asset)
        session.flush()
        return asset.id


def _draft(
    service: PostService,
    account_id: int,
    asset_id: int,
    *,
    post_type: str,
    settings: dict[str, Any],
) -> tuple[int, int, str]:
    view = service.create_post(
        local_owner(),
        PostCreate(
            title="Clip",
            body="a caption",
            media_ids=[asset_id],
            targets=[
                TargetInput(
                    platform=Platform.tiktok,
                    account_id=account_id,
                    post_type=post_type,
                    settings=settings,
                    planned_at=NOW + timedelta(hours=1),
                )
            ],
        ),
    )
    target = view.targets[0]
    assert target.releasable, target.issues
    return view.id, target.id, target.payload_hash


def test_tiktok_draft_release_needs_the_echoed_consent(
    database: Database, settings: Settings, clock: Clock
) -> None:
    publisher = FakeTikTokPublisher("tiktok_inbox", frozenset({"tiktok_draft"}), kind="draft")
    service = _service(database, settings, clock, publisher)
    account_id = _account(database, scopes=["video.upload"])
    asset_id = _video_asset(database)
    post_id, target_id, payload_hash = _draft(
        service, account_id, asset_id, post_type="tiktok_draft", settings={}
    )

    for consent in (None, "I agree"):
        with pytest.raises(Exception) as raised:
            service.release_target(
                local_owner(),
                post_id,
                target_id,
                ReleaseRequest(payload_hash=payload_hash, confirmed=True, consent=consent),
            )
        assert getattr(raised.value, "code", None) == "validation_failed"

    view = service.release_target(
        local_owner(),
        post_id,
        target_id,
        ReleaseRequest(payload_hash=payload_hash, confirmed=True, consent=DRAFT_NOTICE),
    )
    assert view.targets[0].state == "queued"


def test_tiktok_video_release_echoes_the_music_consent(
    database: Database, settings: Settings, clock: Clock
) -> None:
    settings = settings.model_copy(update={"tiktok_app_status": "audited"})
    publisher = FakeTikTokPublisher("tiktok_direct", frozenset({"tiktok_video"}))
    service = _service(database, settings, clock, publisher)
    account_id = _account(database, scopes=["video.publish"])
    asset_id = _video_asset(database)
    post_id, target_id, payload_hash = _draft(
        service,
        account_id,
        asset_id,
        post_type="tiktok_video",
        settings={"privacy_level": "SELF_ONLY"},
    )

    with pytest.raises(Exception) as raised:
        service.release_target(
            local_owner(),
            post_id,
            target_id,
            ReleaseRequest(payload_hash=payload_hash, confirmed=True, consent="whatever"),
        )
    assert getattr(raised.value, "code", None) == "validation_failed"

    view = service.release_target(
        local_owner(),
        post_id,
        target_id,
        ReleaseRequest(payload_hash=payload_hash, confirmed=True, consent=CONSENT),
    )
    assert view.targets[0].state == "queued"


def test_branded_content_echoes_the_branded_consent_line(
    database: Database, settings: Settings, clock: Clock
) -> None:
    settings = settings.model_copy(update={"tiktok_app_status": "audited"})
    publisher = FakeTikTokPublisher("tiktok_direct", frozenset({"tiktok_video"}))
    service = _service(database, settings, clock, publisher)
    account_id = _account(database, scopes=["video.publish"])
    asset_id = _video_asset(database)
    post_id, target_id, payload_hash = _draft(
        service,
        account_id,
        asset_id,
        post_type="tiktok_video",
        settings={
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "commercial_content": True,
            "branded_content": True,
        },
    )
    branded = (
        "By posting, you agree to TikTok's Branded Content Policy and Music Usage Confirmation."
    )
    with pytest.raises(Exception) as raised:
        service.release_target(
            local_owner(),
            post_id,
            target_id,
            ReleaseRequest(payload_hash=payload_hash, confirmed=True, consent=CONSENT),
        )
    assert getattr(raised.value, "code", None) == "validation_failed"
    view = service.release_target(
        local_owner(),
        post_id,
        target_id,
        ReleaseRequest(payload_hash=payload_hash, confirmed=True, consent=branded),
    )
    assert view.targets[0].state == "queued"
    with database.session() as session:
        jobs = session.scalars(select(PublishJob).where(PublishJob.kind == "send")).all()
        assert len(jobs) == 1


def test_branded_content_with_private_visibility_cannot_be_released(
    database: Database, settings: Settings, clock: Clock
) -> None:
    settings = settings.model_copy(update={"tiktok_app_status": "audited"})
    publisher = FakeTikTokPublisher("tiktok_direct", frozenset({"tiktok_video"}))
    service = _service(database, settings, clock, publisher)
    account_id = _account(database, scopes=["video.publish"])
    asset_id = _video_asset(database)
    view = service.create_post(
        local_owner(),
        PostCreate(
            title="Clip",
            media_ids=[asset_id],
            targets=[
                TargetInput(
                    platform=Platform.tiktok,
                    account_id=account_id,
                    post_type="tiktok_video",
                    settings={
                        "privacy_level": "SELF_ONLY",
                        "commercial_content": True,
                        "branded_content": True,
                    },
                    planned_at=NOW + timedelta(hours=1),
                )
            ],
        ),
    )
    target = view.targets[0]
    assert not target.releasable
    assert "branded_content_private" in {issue.code for issue in target.issues}
    with pytest.raises(Exception) as raised:
        service.release_target(
            local_owner(),
            view.id,
            target.id,
            ReleaseRequest(payload_hash=target.payload_hash, confirmed=True, consent=CONSENT),
        )
    assert getattr(raised.value, "status_code", 200) in {409, 422}


def test_tiktok_release_by_a_key_is_human_required(
    database: Database, settings: Settings, clock: Clock
) -> None:
    from runway.auth.principal import ALL_SCOPES, Principal

    key = Principal(
        kind="agent",
        id="key-9",
        workspace_id=1,
        role=None,
        scopes=ALL_SCOPES,
        display="agent key",
    )
    publisher = FakeTikTokPublisher("tiktok_inbox", frozenset({"tiktok_draft"}), kind="draft")
    service = _service(database, settings, clock, publisher)
    account_id = _account(database, scopes=["video.upload"])
    asset_id = _video_asset(database)
    post_id, target_id, payload_hash = _draft(
        service, account_id, asset_id, post_type="tiktok_draft", settings={}
    )
    with pytest.raises(Exception) as raised:
        service.release_target(
            key,
            post_id,
            target_id,
            ReleaseRequest(payload_hash=payload_hash, confirmed=True, consent=DRAFT_NOTICE),
        )
    assert getattr(raised.value, "code", None) == "human_required"
    assert getattr(raised.value, "status_code", 0) == 403


def test_consent_echo_is_not_required_for_other_platforms(
    database: Database, settings: Settings, clock: Clock
) -> None:
    @dataclass
    class FakeYouTube:
        name: str = "youtube_post"
        kind: str = "direct"
        platform: Platform = Platform.youtube
        supported: frozenset[str] = frozenset({"youtube_post"})

        def supports(self, post_type: str) -> bool:
            return post_type in self.supported

        def preflight(self, ctx: PublishContext) -> list[Issue]:
            return []

        def send(self, ctx: PublishContext, progress: Progress) -> SendResult:
            ctx.mark_request_sent()
            return SendResult(status="published", external_id="p1")

        def poll(self, ctx: PublishContext) -> PollResult:
            return PollResult(status="published")

        def verify(self, ctx: PublishContext) -> VerifyResult:
            return VerifyResult(verified=True)

        def reconcile(self, ctx: PublishContext) -> ReconcileResult:
            return ReconcileResult(status="absent", checked=("the fake platform",))

    settings = settings.model_copy(update={"youtube_posts_api_access": True})
    publisher = FakeYouTube()
    registry = PublisherRegistry()
    registry.register(publisher)
    service = PostService(database, settings, registry, clock=clock)
    with database.session() as session:
        account = ConnectedAccount(
            platform="youtube",
            external_account_id="UC-1",
            handle="@yt",
            status="connected",
            granted_scopes_json=json.dumps(["https://www.googleapis.com/auth/youtube.force-ssl"]),
            token_ref=None,
            metadata_json="{}",
        )
        session.add(account)
        session.flush()
        account_id = account.id
    view = service.create_post(
        local_owner(),
        PostCreate(
            title="Post",
            body="text",
            targets=[
                TargetInput(
                    platform=Platform.youtube,
                    account_id=account_id,
                    post_type="youtube_post",
                    planned_at=NOW + timedelta(hours=1),
                )
            ],
        ),
    )
    target = view.targets[0]
    assert target.releasable
    released = service.release_target(
        local_owner(),
        view.id,
        target.id,
        ReleaseRequest(payload_hash=target.payload_hash, confirmed=True),
    )
    assert released.targets[0].state == "queued"
    with database.session() as session:
        assert session.scalars(select(DraftTarget.id)).all()
        assert session.scalars(select(DraftMedia.id)).all() == []
