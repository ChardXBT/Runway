"""The real TikTok publishers inside the publishing engine (spec 08 section 4).

`FakeTikTok` answers the Content Posting API over `httpx.MockTransport` while
the harness drives real releases through `PostService` and the worker: send,
poll (`SEND_TO_USER_INBOX` -> `needs_user` -> `published`), verify, and the
rate guards (5 pending inbox shares, ~15 direct posts per 24 h).
"""

from __future__ import annotations

import hashlib
import io
import json
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import select

from runway.auth.principal import local_owner
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    AccountPublisher,
    ConnectedAccount,
    ContentDraft,
    DraftTarget,
    MediaAssetV2,
    PublishJob,
    TargetAttempt,
)
from runway.jobs.queue import JobQueue
from runway.jobs.worker import Worker
from runway.mediastore.probe import ProbeResult
from runway.platforms.assisted_publisher import AssistedPublisher
from runway.platforms.base import TokenSet
from runway.platforms.capabilities import Platform
from runway.platforms.publisher_registry import PublisherRegistry
from runway.platforms.tiktok import TikTokProvider
from runway.platforms.tiktok_publisher import TikTokDirectPublisher, TikTokInboxPublisher
from runway.posts.engine import PublishingEngine
from runway.posts.schemas import PostCreate, ReleaseRequest, TargetInput
from runway.posts.service import PostService

NOW = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
VIDEO = b"fake-video" * 2_100_000  # 21 MB: a 10 MB chunk plus an 11 MB tail
DRAFT_NOTICE = "This sends a draft to your TikTok inbox. You finish and post it in the app."
CONSENT = "By posting, you agree to TikTok's Music Usage Confirmation"

PROBE = ProbeResult(
    kind="video",
    container="mp4",
    format_names=("mp4",),
    size_bytes=len(VIDEO),
    duration_ms=45_000,
    bitrate=None,
    video_codec="h264",
    width=1080,
    height=1920,
    rotation=0,
    fps=30.0,
    audio_codec="aac",
    audio_sample_rate=48_000,
    audio_channels=2,
    moov_at_front=True,
    has_edit_list=False,
).to_dict()

CREATOR_INFO = {
    "creator_nickname": "Owner",
    "creator_username": "owner",
    "creator_avatar_url": "https://cdn.example.test/a.png",
    "privacy_level_options": ["PUBLIC_TO_EVERYONE", "MUTUAL_FOLLOW_FRIENDS", "SELF_ONLY"],
    "comment_disabled": False,
    "duet_disabled": False,
    "stitch_disabled": False,
    "max_video_post_duration_sec": 600,
}


def _ok(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200, json={"data": data, "error": {"code": "ok", "message": "", "log_id": "log-1"}}
    )


class FakeTikTok:
    """The Content Posting API on `httpx.MockTransport`.

    `events` records API calls in order. The last scripted status for a
    publish id repeats, the way a real status stays put until the next
    transition.
    """

    def __init__(self) -> None:
        self.events: list[str] = []
        self.init_bodies: list[dict[str, Any]] = []
        self.uploads: dict[str, list[tuple[str, int]]] = {}
        self.status_calls: list[str] = []
        self.statuses: dict[str, deque[dict[str, Any]]] = {}
        self.creator: dict[str, Any] = dict(CREATOR_INFO)
        self._sessions = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/creator_info/query/"):
            self.events.append("creator_info")
            return _ok(dict(self.creator))
        if request.method == "POST" and path.endswith("/video/init/"):
            self._sessions += 1
            self.events.append("init")
            self.init_bodies.append(json.loads(request.content))
            publish_id = f"pub-{self._sessions}"
            self.uploads[f"https://upload.tiktokapis.com/session/{publish_id}"] = []
            return _ok(
                {
                    "publish_id": publish_id,
                    "upload_url": f"https://upload.tiktokapis.com/session/{publish_id}",
                }
            )
        if request.method == "POST" and path.endswith("/status/fetch/"):
            self.events.append("status")
            publish_id = str(json.loads(request.content).get("publish_id") or "")
            self.status_calls.append(publish_id)
            queue = self.statuses.get(publish_id)
            if queue:
                item = queue.popleft() if len(queue) > 1 else queue[0]
            else:
                item = {"status": "PROCESSING_UPLOAD"}
            return _ok(dict(item))
        if request.method == "PUT":
            self.events.append("put")
            ranges = self.uploads.get(str(request.url))
            if ranges is None:
                return httpx.Response(404)
            content_range = request.headers.get("Content-Range", "")
            ranges.append((content_range, len(request.content)))
            end = int(content_range.removeprefix("bytes ").split("-")[1].split("/")[0])
            total = int(content_range.rsplit("/", 1)[1])
            return httpx.Response(201 if end == total - 1 else 206)
        return httpx.Response(404)


class _BytesStore:
    """Serves the same bytes for every media key (FILE_UPLOAD needs no URL)."""

    kind = "local"

    def __init__(self, data: bytes) -> None:
        self._data = data

    def open(self, key: str, *, workspace_id: str | int) -> io.BytesIO:
        return io.BytesIO(self._data)

    def signed_get_url(self, key: str, ttl_seconds: int, *, workspace_id: str | int) -> None:
        return None


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now

    def advance(self, delta: timedelta) -> None:
        self.now += delta


@dataclass
class Harness:
    database: Database
    settings: Settings
    clock: Clock
    fake: FakeTikTok

    def registry(self) -> PublisherRegistry:
        registry = PublisherRegistry()
        registry.register(AssistedPublisher(self.database))
        provider = TikTokProvider(
            self.settings, httpx.Client(transport=httpx.MockTransport(self.fake))
        )
        registry.register(TikTokInboxPublisher(self.settings, provider=provider, clock=self.clock))
        registry.register(TikTokDirectPublisher(self.settings, provider=provider, clock=self.clock))
        return registry

    def _token(self, account: ConnectedAccount) -> TokenSet:
        return TokenSet(access_token="tok-1", scopes=["video.upload", "video.publish"])

    def service(self) -> PostService:
        return PostService(
            self.database,
            self.settings,
            self.registry(),
            clock=self.clock,
            token_provider=self._token,
            media_stores={"local": _BytesStore(VIDEO)},
        )

    def engine(self) -> PublishingEngine:
        return PublishingEngine(
            self.database,
            self.settings,
            self.registry(),
            clock=self.clock,
            token_provider=self._token,
            media_stores={"local": _BytesStore(VIDEO)},
        )

    def queue(self, engine: PublishingEngine) -> JobQueue:
        return JobQueue(
            self.database, clock=self.clock, expired_send_handler=engine.handle_expired_send
        )

    def worker(self) -> Worker:
        engine = self.engine()
        queue = self.queue(engine)
        return Worker(queue=queue, handlers=engine.handlers(), worker_id="w")

    def account(self, scopes: list[str], *, publisher_for: dict[str, str] | None = None) -> int:
        with self.database.session() as session:
            account = ConnectedAccount(
                platform="tiktok",
                external_account_id="open-1",
                handle="@owner",
                status="connected",
                granted_scopes_json=json.dumps(scopes),
                metadata_json="{}",
            )
            session.add(account)
            session.flush()
            for post_type, publisher in (publisher_for or {}).items():
                session.add(
                    AccountPublisher(
                        account_id=account.id, post_type=post_type, publisher=publisher
                    )
                )
            return account.id

    def asset(self) -> int:
        with self.database.session() as session:
            asset = MediaAssetV2(
                workspace_id=1,
                kind="video",
                content_type="video/mp4",
                size_bytes=len(VIDEO),
                sha256=hashlib.sha256(VIDEO).hexdigest(),
                storage="local",
                storage_key="w/1/videos/clip.mp4",
                status="ready",
                probe_json=json.dumps(PROBE),
            )
            session.add(asset)
            session.flush()
            return asset.id

    def prepare(
        self,
        service: PostService,
        *,
        account_id: int,
        post_type: str,
        settings: dict[str, Any] | None = None,
    ) -> tuple[int, int, str]:
        view = service.create_post(
            local_owner(),
            PostCreate(
                title="Clip",
                body="a caption",
                media_ids=[self.asset()],
                targets=[
                    TargetInput(
                        platform=Platform.tiktok,
                        account_id=account_id,
                        post_type=post_type,
                        settings=dict(settings or {}),
                        planned_at=self.clock.now,
                    )
                ],
            ),
        )
        target = view.targets[0]
        return view.id, target.id, target.payload_hash

    def release(
        self,
        *,
        post_type: str = "tiktok_draft",
        consent: str,
        scopes: list[str] | None = None,
        account_id: int | None = None,
        settings: dict[str, Any] | None = None,
    ) -> int:
        if account_id is None:
            account_id = self.account(scopes or ["video.upload"])
        service = self.service()
        post_id, target_id, payload_hash = self.prepare(
            service, account_id=account_id, post_type=post_type, settings=settings
        )
        service.release_target(
            local_owner(),
            post_id,
            target_id,
            ReleaseRequest(payload_hash=payload_hash, confirmed=True, consent=consent),
        )
        return target_id

    def pending_draft(self, account_id: int, index: int) -> None:
        """A target already waiting in the creator's inbox (for the share cap)."""
        with self.database.session() as session:
            post = ContentDraft(title=f"Draft {index}", body="", status="draft")
            session.add(post)
            session.flush()
            session.add(
                DraftTarget(
                    workspace_id=1,
                    draft_id=post.id,
                    platform="tiktok",
                    account_id=account_id,
                    post_type="tiktok_draft",
                    publisher="tiktok_inbox",
                    status="pending",
                    state="needs_user",
                    released_at=self.clock.now,
                )
            )

    def drain(self, rounds: int = 80) -> None:
        for _ in range(rounds):
            worker = self.worker()
            if worker.run_once():
                continue
            engine = self.engine()
            queue = self.queue(engine)
            due = queue.next_run_at(worker.handlers.keys())
            if due is None:
                return
            self.clock.now = max(self.clock.now, due) + timedelta(seconds=1)
        raise AssertionError("the queue did not drain")

    def target(self, target_id: int) -> DraftTarget:
        with self.database.session() as session:
            row = session.get(DraftTarget, target_id)
            assert row is not None
            session.expunge(row)
            return row

    def attempts(self, target_id: int) -> list[tuple[str, str]]:
        with self.database.session() as session:
            return [
                (row.kind, row.outcome)
                for row in session.scalars(
                    select(TargetAttempt)
                    .where(TargetAttempt.target_id == target_id)
                    .order_by(TargetAttempt.id)
                )
            ]


@pytest.fixture()
def harness(database: Database, settings: Settings) -> Harness:
    return Harness(database, settings, Clock(), FakeTikTok())


def test_inbox_draft_walks_inbox_to_published(harness: Harness) -> None:
    harness.fake.statuses["pub-1"] = deque(
        [
            {"status": "SEND_TO_USER_INBOX"},
            {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": "7390001"},
        ]
    )
    target_id = harness.release(consent=DRAFT_NOTICE)
    harness.drain()
    target = harness.target(target_id)
    assert target.state == "verified"
    assert target.external_id == "7390001"
    assert target.published_url == "https://www.tiktok.com/@owner/video/7390001"
    assert harness.attempts(target_id) == [
        ("send", "processing"),
        ("poll", "published"),
        ("verify", "verified"),
    ]
    with harness.database.session() as session:
        send = session.scalar(select(TargetAttempt).where(TargetAttempt.kind == "send"))
        assert send is not None and send.request_sent_at is not None
        assert "tok-1" not in (send.resumable_json or "")


def test_direct_publishes_when_the_app_is_audited(database: Database, settings: Settings) -> None:
    settings = settings.model_copy(update={"tiktok_app_status": "audited"})
    harness = Harness(database, settings, Clock(), FakeTikTok())
    harness.fake.statuses["pub-1"] = deque(
        [
            {"status": "PROCESSING_UPLOAD"},
            {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": "7390002"},
        ]
    )
    account_id = harness.account(["video.publish"])
    service = harness.service()
    post_id, target_id, payload_hash = harness.prepare(
        service,
        account_id=account_id,
        post_type="tiktok_video",
        settings={"privacy_level": "SELF_ONLY"},
    )
    service.release_target(
        local_owner(),
        post_id,
        target_id,
        ReleaseRequest(payload_hash=payload_hash, confirmed=True, consent=CONSENT),
    )
    harness.drain()
    target = harness.target(target_id)
    assert target.state == "verified"
    assert target.external_id == "7390002"
    assert harness.fake.events[0] == "creator_info", "preflight re-reads creator info"
    body = harness.fake.init_bodies[0]
    assert body["post_info"]["privacy_level"] == "SELF_ONLY"
    assert body["post_info"]["disable_comment"] is True
    assert body["post_info"]["is_aigc"] is False
    with harness.database.session() as session:
        send = session.scalar(select(TargetAttempt).where(TargetAttempt.kind == "send"))
        assert send is not None and send.request_sent_at is not None


def test_direct_is_never_sent_below_audited(harness: Harness) -> None:
    account_id = harness.account(["video.publish"], publisher_for={"tiktok_video": "tiktok_direct"})
    service = harness.service()
    view = service.create_post(
        local_owner(),
        PostCreate(
            title="Clip",
            media_ids=[harness.asset()],
            targets=[
                TargetInput(
                    platform=Platform.tiktok,
                    account_id=account_id,
                    post_type="tiktok_video",
                    settings={"privacy_level": "SELF_ONLY"},
                    planned_at=harness.clock.now,
                )
            ],
        ),
    )
    target = view.targets[0]
    assert not target.releasable
    assert "publisher_not_ready" in {issue.code for issue in target.issues}
    with pytest.raises(Exception) as raised:
        service.release_target(
            local_owner(),
            view.id,
            target.id,
            ReleaseRequest(payload_hash=target.payload_hash, confirmed=True, consent=CONSENT),
        )
    # The target never became ready, so releasing it is refused outright.
    assert getattr(raised.value, "code", None) == "invalid_state"
    assert harness.fake.events == [], "no platform call without the audit"


def test_five_pending_drafts_block_the_sixth_release(harness: Harness) -> None:
    account_id = harness.account(["video.upload"])
    for index in range(5):
        harness.pending_draft(account_id, index)
    service = harness.service()
    post_id, target_id, payload_hash = harness.prepare(
        service, account_id=account_id, post_type="tiktok_draft"
    )
    with pytest.raises(Exception) as raised:
        service.release_target(
            local_owner(),
            post_id,
            target_id,
            ReleaseRequest(payload_hash=payload_hash, confirmed=True, consent=DRAFT_NOTICE),
        )
    assert getattr(raised.value, "code", None) == "rate_limited"


def test_a_failed_publish_maps_the_reason_through_the_engine(harness: Harness) -> None:
    harness.fake.statuses["pub-1"] = deque(
        [{"status": "FAILED", "fail_reason": "duration_check_failed"}]
    )
    target_id = harness.release(consent=DRAFT_NOTICE)
    harness.drain()
    target = harness.target(target_id)
    assert target.state == "failed_final"
    assert "longer" in (target.state_reason or "")
    assert harness.attempts(target_id) == [("send", "processing"), ("poll", "failed_final")]


def test_a_retryable_publish_failure_retries_then_stops(harness: Harness) -> None:
    harness.fake.statuses["pub-1"] = deque([{"status": "FAILED", "fail_reason": "internal"}])
    target_id = harness.release(consent=DRAFT_NOTICE)
    harness.drain()
    target = harness.target(target_id)
    assert target.state == "failed_retryable"
    assert "used up" in (target.state_reason or "")
    sends = [attempt for attempt in harness.attempts(target_id) if attempt[0] == "send"]
    assert len(sends) == 4  # the release send plus three bounded retries
    with harness.database.session() as session:
        pending = session.scalars(select(PublishJob).where(PublishJob.state == "pending")).all()
    assert pending == []
