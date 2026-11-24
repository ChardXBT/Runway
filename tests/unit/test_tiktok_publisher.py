"""The live TikTok publishers: init, ordered chunks, status fetch (spec 08 section 4).

`FakeTikTok` answers the Content Posting API over `httpx.MockTransport`: init
hands out publish ids and one-hour upload URLs, `PUT`s record their
`Content-Range` and answer 206/201 (or 403 once the session lapses), and
`status/fetch` replays scripted statuses. No request leaves the process.
"""

from __future__ import annotations

import io
import json
from collections import deque
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from runway.config import Settings
from runway.mediastore.probe import ProbeResult
from runway.platforms.base import ProviderError, TokenSet
from runway.platforms.capabilities import Platform
from runway.platforms.publisher import (
    AccountRef,
    Issue,
    MediaRef,
    NullProgress,
    PublishContext,
    PublishRefused,
    PublishRetryLater,
)
from runway.platforms.tiktok import TikTokProvider
from runway.platforms.tiktok_publisher import TikTokDirectPublisher, TikTokInboxPublisher

NOW = datetime(2026, 12, 1, 12, 0, tzinfo=UTC)
VIDEO = b"fake-video" * 2_100_000  # 21 MB: a 10 MB chunk plus an 11 MB tail
PAYLOAD_HASH = "a" * 64
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


def _error(code: str, message: str, status: int) -> httpx.Response:
    return httpx.Response(
        status,
        json={"data": {}, "error": {"code": code, "message": message, "log_id": "log-1"}},
    )


class FakeTikTok:
    """The Content Posting API on `httpx.MockTransport`.

    `events` records API calls in order; a test appends "marker" to the same
    list from `ctx.mark_request_sent` to prove the marker precedes the init.
    The last scripted status for a publish id repeats, the way a real status
    stays put until the next transition.
    """

    def __init__(self) -> None:
        self.events: list[str] = []
        self.init_bodies: list[dict[str, Any]] = []
        self.uploads: dict[str, dict[str, Any]] = {}
        self.status_calls: list[str] = []
        self.statuses: dict[str, deque[dict[str, Any]]] = {}
        self.creator: dict[str, Any] = dict(CREATOR_INFO)
        self.init_error: tuple[int, str, str] | None = None
        self.expire_put_after: int | None = None
        self.always_expire = False
        self.no_upload_url = False
        self._sessions = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path.endswith("/creator_info/query/"):
            self.events.append("creator_info")
            return _ok(dict(self.creator))
        if request.method == "POST" and path.endswith("/video/init/"):
            self._sessions += 1
            self.events.append("init")
            body = json.loads(request.content)
            body["_path"] = path
            self.init_bodies.append(body)
            if self.init_error is not None:
                status, code, message = self.init_error
                return _error(code, message, status)
            publish_id = f"pub-{self._sessions}"
            url = f"https://upload.tiktokapis.com/session/{publish_id}"
            self.uploads[url] = {"ranges": [], "expired": False, "session": self._sessions}
            if self.no_upload_url:
                return _ok({"publish_id": publish_id})
            return _ok({"publish_id": publish_id, "upload_url": url})
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
            state = self.uploads.get(str(request.url))
            if state is None:
                return httpx.Response(404)
            self.events.append("put")
            if state["expired"] or self.always_expire:
                return httpx.Response(403)
            content_range = request.headers.get("Content-Range", "")
            state["ranges"].append((content_range, len(request.content)))
            if (
                self.expire_put_after is not None
                and state.get("session") == 1
                and len(state["ranges"]) >= self.expire_put_after
            ):
                state["expired"] = True  # the first session's URL lapses; re-init heals
            end = int(content_range.removeprefix("bytes ").split("-")[1].split("/")[0])
            total = int(content_range.rsplit("/", 1)[1])
            return httpx.Response(201 if end == total - 1 else 206)
        return httpx.Response(404)


@pytest.fixture()
def fake() -> FakeTikTok:
    return FakeTikTok()


def _provider(settings: Settings, fake: FakeTikTok) -> TikTokProvider:
    return TikTokProvider(settings, httpx.Client(transport=httpx.MockTransport(fake)))


@pytest.fixture()
def inbox(settings: Settings, fake: FakeTikTok) -> TikTokInboxPublisher:
    return TikTokInboxPublisher(settings, provider=_provider(settings, fake))


@pytest.fixture()
def direct(settings: Settings, fake: FakeTikTok) -> TikTokDirectPublisher:
    audited = settings.model_copy(update={"tiktok_app_status": "audited"})
    return TikTokDirectPublisher(audited, provider=_provider(audited, fake))


class Ctx:
    """A worker-wired context plus the hooks' recordings."""

    def __init__(self, fake: FakeTikTok, **kwargs: Any) -> None:
        self.marked: list[datetime] = []
        self.saved: list[dict[str, Any]] = []
        post_type = kwargs.pop("post_type", "tiktok_draft")
        publisher = kwargs.pop("publisher", "tiktok_inbox")
        scopes = kwargs.pop("scopes", ("video.upload", "video.publish"))
        media_size = kwargs.pop("media_size", len(VIDEO))
        probe = kwargs.pop("probe", PROBE)
        media = kwargs.pop(
            "media",
            (
                MediaRef(
                    asset_id=11,
                    position=0,
                    kind="video",
                    sha256="0" * 64,
                    size_bytes=media_size,
                    content_type="video/mp4",
                    storage="local",
                    storage_key="w/1/videos/clip.mp4",
                    probe=probe,
                ),
            ),
        )

        def mark() -> datetime:
            self.marked.append(NOW)
            fake.events.append("marker")
            return NOW

        ctx = PublishContext(
            workspace_id=1,
            post_id=5,
            target_id=7,
            platform=Platform.tiktok,
            post_type=post_type,
            publisher=publisher,
            account=AccountRef(
                id=3,
                platform=Platform.tiktok,
                external_account_id="open-1",
                handle="@owner",
                status="connected",
                granted_scopes=tuple(scopes),
                token_ref="ref-1",
            ),
            title="Clip",
            caption="a caption",
            settings=dict(kwargs.pop("settings", {})),
            media=media,
            planned_at=NOW,
            payload_hash=kwargs.pop("payload_hash", PAYLOAD_HASH),
            ai_generated=kwargs.pop("ai_generated", False),
            external_id=kwargs.pop("external_id", None),
            resumable=dict(kwargs.pop("resumable", {})),
            request_sent_at=kwargs.pop("request_sent_at", None),
            clock=kwargs.pop("clock", lambda: NOW),
            _mark_request_sent=mark,
            _save_resumable=lambda state: self.saved.append(dict(state)),
            _open_media=lambda _media: io.BytesIO(VIDEO),
            _signed_media_url=lambda _media, _ttl: None,
            _access_token=lambda: TokenSet(access_token="tok-1", scopes=list(scopes)),
        )
        self.ctx = ctx


def _upload_url(fake: FakeTikTok, publish_id: str = "pub-9") -> str:
    url = f"https://upload.tiktokapis.com/session/{publish_id}"
    fake.uploads[url] = {"ranges": [], "expired": False}
    return url


def _resumable(fake: FakeTikTok, **overrides: Any) -> dict[str, Any]:
    state = {
        "payload_hash": PAYLOAD_HASH,
        "video_size": len(VIDEO),
        "publish_id": "pub-9",
        "upload_url": _upload_url(fake),
        "expires_at": (NOW + timedelta(minutes=50)).isoformat(),
        "next_chunk": 1,
    }
    state.update(overrides)
    return state


# ---------------------------------------------------------------- send


def test_inbox_send_inits_uploads_and_reports_processing(
    inbox: TikTokInboxPublisher, fake: FakeTikTok
) -> None:
    holder = Ctx(fake)
    result = inbox.send(holder.ctx, NullProgress())
    assert result.status == "processing"
    assert result.external_id == "pub-1"
    body = fake.init_bodies[0]
    assert body["_path"].endswith("/post/publish/inbox/video/init/")
    assert body["source_info"] == {
        "source": "FILE_UPLOAD",
        "video_size": len(VIDEO),
        "chunk_size": 10_000_000,
        "total_chunk_count": 2,
    }
    upload = next(iter(fake.uploads.values()))
    assert [entry[0] for entry in upload["ranges"]] == [
        "bytes 0-9999999/21000000",
        "bytes 10000000-20999999/21000000",
    ]
    assert fake.events == ["marker", "init", "put", "put"]
    assert len(holder.marked) == 1, "the request marker is committed exactly once"
    assert holder.ctx.resumable["publish_id"] == "pub-1"
    assert holder.ctx.resumable["next_chunk"] == 2


def test_direct_send_posts_the_mandatory_screen_choices(
    direct: TikTokDirectPublisher, fake: FakeTikTok
) -> None:
    holder = Ctx(
        fake,
        post_type="tiktok_video",
        publisher="tiktok_direct",
        ai_generated=True,
        settings={
            "privacy_level": "PUBLIC_TO_EVERYONE",
            "allow_comment": True,
            "allow_duet": False,
            "allow_stitch": True,
            "commercial_content": True,
            "branded_content": True,
            "your_brand": True,
            "cover_timestamp_ms": 1200,
            "is_aigc": True,
        },
    )
    result = direct.send(holder.ctx, NullProgress())
    assert result.status == "processing"
    body = fake.init_bodies[0]
    assert body["_path"].endswith("/post/publish/video/init/")
    assert body["post_info"] == {
        "title": "a caption",
        "privacy_level": "PUBLIC_TO_EVERYONE",
        "disable_comment": False,
        "disable_duet": True,
        "disable_stitch": False,
        "video_cover_timestamp_ms": 1200,
        "is_aigc": True,
        "brand_content_toggle": True,
        "brand_organic_toggle": True,
    }


def test_an_expired_upload_url_restarts_from_init(
    inbox: TikTokInboxPublisher, fake: FakeTikTok
) -> None:
    fake.expire_put_after = 1  # the first chunk lands, then the URL lapses
    holder = Ctx(fake)
    result = inbox.send(holder.ctx, NullProgress())
    assert result.status == "processing"
    assert result.external_id == "pub-2"
    assert fake.events == ["marker", "init", "put", "put", "init", "put", "put"]
    first, second = fake.uploads.values()
    assert first["ranges"] == [("bytes 0-9999999/21000000", 10_000_000)]
    assert [entry[0] for entry in second["ranges"]] == [
        "bytes 0-9999999/21000000",
        "bytes 10000000-20999999/21000000",
    ]
    assert len(holder.marked) == 1, "re-init does not move the point-of-no-return marker"
    assert holder.ctx.resumable["publish_id"] == "pub-2"


def test_a_resumed_send_continues_from_the_saved_chunk(
    inbox: TikTokInboxPublisher, fake: FakeTikTok
) -> None:
    holder = Ctx(fake, resumable=_resumable(fake), request_sent_at=NOW)
    result = inbox.send(holder.ctx, NullProgress())
    assert result.external_id == "pub-9"
    assert fake.events == ["put"], "no new init; the upload resumes in place"
    assert holder.marked == [], "the marker was already committed"
    upload = fake.uploads["https://upload.tiktokapis.com/session/pub-9"]
    assert [entry[0] for entry in upload["ranges"]] == ["bytes 10000000-20999999/21000000"]


def test_a_resumed_send_with_a_dead_url_reinits(
    inbox: TikTokInboxPublisher, fake: FakeTikTok
) -> None:
    holder = Ctx(
        fake,
        resumable=_resumable(fake, expires_at=(NOW - timedelta(minutes=1)).isoformat()),
        request_sent_at=NOW,
    )
    result = inbox.send(holder.ctx, NullProgress())
    assert result.external_id == "pub-1"
    assert fake.events == ["init", "put", "put"]
    assert holder.marked == []


def test_a_resumable_from_another_payload_is_ignored(
    inbox: TikTokInboxPublisher, fake: FakeTikTok
) -> None:
    holder = Ctx(fake, resumable=_resumable(fake, payload_hash="b" * 64))
    result = inbox.send(holder.ctx, NullProgress())
    assert result.external_id == "pub-1"
    assert "init" in fake.events


def test_persistent_dead_upload_urls_stop_after_bounded_restarts(
    inbox: TikTokInboxPublisher, fake: FakeTikTok
) -> None:
    fake.always_expire = True  # every fresh URL refuses; the send must not spin
    holder = Ctx(fake)
    with pytest.raises(PublishRetryLater):
        inbox.send(holder.ctx, NullProgress())
    assert fake.events.count("init") <= 4


def test_an_init_without_an_upload_url_is_a_provider_error(
    inbox: TikTokInboxPublisher, fake: FakeTikTok
) -> None:
    fake.no_upload_url = True
    holder = Ctx(fake)
    with pytest.raises(ProviderError, match="upload_url"):
        inbox.send(holder.ctx, NullProgress())
    assert "put" not in fake.events, "a malformed init must stop before any PUT"


def test_the_pending_share_cap_is_a_final_refusal(
    inbox: TikTokInboxPublisher, fake: FakeTikTok
) -> None:
    fake.init_error = (403, "spam_risk_too_many_pending_share", "too many pending shares")
    holder = Ctx(fake)
    with pytest.raises(PublishRefused) as raised:
        inbox.send(holder.ctx, NullProgress())
    assert raised.value.code == "spam_risk_too_many_pending_share"
    assert raised.value.final


def test_the_daily_post_cap_is_a_final_refusal(
    direct: TikTokDirectPublisher, fake: FakeTikTok
) -> None:
    fake.init_error = (
        403,
        "spam_risk_too_many_posts",
        "the creator posted too much today",
    )
    holder = Ctx(fake, post_type="tiktok_video", publisher="tiktok_direct")
    with pytest.raises(PublishRefused):
        direct.send(holder.ctx, NullProgress())


def test_resumable_and_response_never_hold_secrets(
    inbox: TikTokInboxPublisher, fake: FakeTikTok
) -> None:
    holder = Ctx(fake)
    result = inbox.send(holder.ctx, NullProgress())
    blob = json.dumps(holder.ctx.resumable) + json.dumps(dict(result.response))
    assert "tok-1" not in blob
    assert "access_token" not in blob
    assert "refresh_token" not in blob


# ---------------------------------------------------------------- preflight


def _errors(issues: list[Issue]) -> set[str]:
    return {issue.code for issue in issues if issue.severity == "error"}


def test_inbox_preflight_needs_the_upload_scope(
    inbox: TikTokInboxPublisher, fake: FakeTikTok
) -> None:
    assert inbox.preflight(Ctx(fake, scopes=("video.upload",)).ctx) == []
    issues = inbox.preflight(Ctx(fake, scopes=()).ctx)
    assert "missing_scope" in _errors(issues)


def test_direct_preflight_needs_the_publish_scope(
    direct: TikTokDirectPublisher, fake: FakeTikTok
) -> None:
    ctx = Ctx(fake, post_type="tiktok_video", publisher="tiktok_direct", scopes=())
    assert "missing_scope" in _errors(direct.preflight(ctx.ctx))


def test_direct_preflight_refuses_below_audited(settings: Settings, fake: FakeTikTok) -> None:
    publisher = TikTokDirectPublisher(settings, provider=_provider(settings, fake))
    ctx = Ctx(
        fake,
        post_type="tiktok_video",
        publisher="tiktok_direct",
        settings={"privacy_level": "SELF_ONLY"},
    )
    assert "app_not_audited" in _errors(publisher.preflight(ctx.ctx))
    assert "creator_info" not in fake.events, "the refusal needs no platform call"


def test_direct_preflight_rechecks_the_creator_options(
    direct: TikTokDirectPublisher, fake: FakeTikTok
) -> None:
    ctx = Ctx(
        fake,
        post_type="tiktok_video",
        publisher="tiktok_direct",
        settings={"privacy_level": "SELF_ONLY"},
    )
    assert direct.preflight(ctx.ctx) == []
    assert "creator_info" in fake.events
    fake.creator["privacy_level_options"] = ["SELF_ONLY"]
    public = Ctx(
        fake,
        post_type="tiktok_video",
        publisher="tiktok_direct",
        settings={"privacy_level": "PUBLIC_TO_EVERYONE"},
    )
    assert "privacy_not_offered" in _errors(direct.preflight(public.ctx))


def test_direct_preflight_refuses_an_empty_privacy_options_list(
    direct: TikTokDirectPublisher, fake: FakeTikTok
) -> None:
    # No options at all means there is no valid choice, not a pass.
    fake.creator["privacy_level_options"] = []
    ctx = Ctx(
        fake,
        post_type="tiktok_video",
        publisher="tiktok_direct",
        settings={"privacy_level": "SELF_ONLY"},
    )
    assert "privacy_not_offered" in _errors(direct.preflight(ctx.ctx))


def test_direct_preflight_blocks_interactions_the_creator_disabled(
    direct: TikTokDirectPublisher, fake: FakeTikTok
) -> None:
    fake.creator["comment_disabled"] = True
    ctx = Ctx(
        fake,
        post_type="tiktok_video",
        publisher="tiktok_direct",
        settings={"privacy_level": "SELF_ONLY", "allow_comment": True},
    )
    assert "interaction_unavailable" in _errors(direct.preflight(ctx.ctx))


def test_direct_preflight_checks_the_creator_duration_cap(
    direct: TikTokDirectPublisher, fake: FakeTikTok
) -> None:
    fake.creator["max_video_post_duration_sec"] = 30
    ctx = Ctx(
        fake,
        post_type="tiktok_video",
        publisher="tiktok_direct",
        settings={"privacy_level": "SELF_ONLY"},
    )
    assert "duration_exceeds_creator_limit" in _errors(direct.preflight(ctx.ctx))


def test_preflight_needs_exactly_one_probed_video(
    inbox: TikTokInboxPublisher, fake: FakeTikTok
) -> None:
    empty = Ctx(fake, media=())
    assert "media_count" in _errors(inbox.preflight(empty.ctx))
    unprobed = Ctx(fake, probe={})
    assert "probe_missing" in _errors(inbox.preflight(unprobed.ctx))


# ---------------------------------------------------------------- poll


def test_poll_walks_inbox_to_needs_user_then_published(
    inbox: TikTokInboxPublisher, fake: FakeTikTok
) -> None:
    fake.statuses["pub-1"] = deque(
        [
            {"status": "SEND_TO_USER_INBOX"},
            {"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": "7390001"},
        ]
    )
    holder = Ctx(fake, external_id="pub-1", request_sent_at=NOW)
    first = inbox.poll(holder.ctx)
    assert first.status == "needs_user"
    second = inbox.poll(holder.ctx)
    assert second.status == "published"
    assert second.external_id == "7390001"
    assert second.permalink == "https://www.tiktok.com/@owner/video/7390001"


@pytest.mark.parametrize(
    ("reason", "retryable"),
    [
        ("internal", True),
        ("video_pull_failed", True),
        ("file_format_check_failed", False),
        ("duration_check_failed", False),
        ("frame_rate_check_failed", False),
        ("picture_size_check_failed", False),
        ("spam_risk", False),
        ("spam_risk_too_many_posts", False),
        ("auth_removed", False),
        ("publish_cancelled", False),
    ],
)
def test_poll_maps_fail_reasons(
    inbox: TikTokInboxPublisher, fake: FakeTikTok, reason: str, retryable: bool
) -> None:
    fake.statuses["pub-1"] = deque([{"status": "FAILED", "fail_reason": reason}])
    holder = Ctx(fake, external_id="pub-1", request_sent_at=NOW)
    result = inbox.poll(holder.ctx)
    assert result.status == "failed"
    assert result.error_code == reason
    assert result.retryable is retryable


def test_poll_processing_statuses_keep_polling(
    inbox: TikTokInboxPublisher, fake: FakeTikTok
) -> None:
    fake.statuses["pub-1"] = deque([{"status": "PROCESSING_UPLOAD"}])
    holder = Ctx(fake, external_id="pub-1", request_sent_at=NOW)
    result = inbox.poll(holder.ctx)
    assert result.status == "processing"
    assert result.next_poll_seconds == 30


# ---------------------------------------------------------------- verify


def test_verify_reads_back_the_public_post_id(
    inbox: TikTokInboxPublisher, fake: FakeTikTok
) -> None:
    fake.statuses["pub-1"] = deque(
        [{"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": "7390001"}]
    )
    holder = Ctx(fake, external_id="pub-1", request_sent_at=NOW)
    result = inbox.verify(holder.ctx)
    assert result.verified is True
    assert result.permalink == "https://www.tiktok.com/@owner/video/7390001"


def test_verify_waits_while_the_post_is_not_public(
    inbox: TikTokInboxPublisher, fake: FakeTikTok
) -> None:
    fake.statuses["pub-1"] = deque([{"status": "PUBLISH_COMPLETE"}])
    holder = Ctx(fake, external_id="pub-1", request_sent_at=NOW)
    assert inbox.verify(holder.ctx).verified is None


# ---------------------------------------------------------------- reconcile


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ({"status": "PUBLISH_COMPLETE", "publicaly_available_post_id": "7390001"}, "published"),
        ({"status": "PROCESSING_UPLOAD"}, "processing"),
        ({"status": "SEND_TO_USER_INBOX"}, "needs_user"),
        ({"status": "FAILED", "fail_reason": "duration_check_failed"}, "absent"),
    ],
)
def test_reconcile_reads_the_publish_status(
    inbox: TikTokInboxPublisher, fake: FakeTikTok, status: dict, expected: str
) -> None:
    fake.statuses["pub-1"] = deque([status])
    holder = Ctx(fake, external_id="pub-1", request_sent_at=NOW)
    result = inbox.reconcile(holder.ctx)
    assert result.status == expected
    assert result.checked


def test_reconcile_before_the_marker_is_absent(
    inbox: TikTokInboxPublisher, fake: FakeTikTok
) -> None:
    holder = Ctx(fake)
    result = inbox.reconcile(holder.ctx)
    assert result.status == "absent"
    assert fake.events == []


def test_reconcile_after_the_marker_without_a_publish_id_is_absent(
    inbox: TikTokInboxPublisher, fake: FakeTikTok
) -> None:
    holder = Ctx(fake, request_sent_at=NOW)
    result = inbox.reconcile(holder.ctx)
    # Spec 08 section 4: no publish id means init never finished, so nothing exists.
    assert result.status == "absent"
    assert fake.events == [], "no publish id means nothing to check"
