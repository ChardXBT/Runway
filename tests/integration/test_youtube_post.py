"""The `youtube_post` adapter against a fake YouTube posts API (httpx.MockTransport).

Spec 08 section 2 "Community posts" (youtube-a package 3): one
`posts.imageUpload` per image (`channelId` query param, raw bytes, keep the
`imageUploadToken`; a fresh upload per post), the point-of-no-return marker
immediately before `posts.insert`, `published` on the response id,
`posts.list` read-back for verify and poll, newest-posts matching for
reconcile, the named-error map and the five-per-channel-per-day guard.

No real network anywhere (invariant 9).
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs
from zoneinfo import ZoneInfo

import httpx
import pytest

from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.platforms.base import ProviderError, TokenSet
from runway.platforms.capabilities import Platform
from runway.platforms.publish_capability import YOUTUBE_ENGAGE_SCOPE
from runway.platforms.publisher import (
    AccountRef,
    MediaRef,
    NullProgress,
    PublishContext,
    PublishError,
    PublishRetryLater,
    RateGuardContext,
    classify_send_failure,
)
from runway.platforms.youtube_post import YouTubePostPublisher

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
ENGAGE = YOUTUBE_ENGAGE_SCOPE
IMAGE_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048


def migrated(settings: Settings) -> Settings:
    initialize_database(settings).engine.dispose()
    return settings


def live_settings(tmp_path: Path, **overrides: object) -> Settings:
    kwargs = {
        "data_dir": tmp_path / "data",
        "platform_provider": "live",
        "youtube_posts_api_access": True,
        "youtube_client_id": "yt-client",
        "youtube_client_secret": "yt-secret",
        **overrides,
    }
    return migrated(Settings(**kwargs))  # type: ignore[arg-type]


class FakePostsAPI:
    """A stateful fake of the `posts` resource endpoints the adapter calls."""

    def __init__(self, *, channel_id: str = "UCexpected") -> None:
        self.channel_id = channel_id
        self.posts: list[dict[str, object]] = []
        self.image_uploads: list[dict[str, object]] = []
        self.insert_bodies: list[dict[str, object]] = []
        self.insert_params: list[dict[str, list[str]]] = []
        self.channels_calls = 0
        self.image_calls = 0
        self.insert_calls = 0
        self.list_calls = 0
        self.insert_error: tuple[int, dict[str, object]] | None = None
        self.insert_failure: BaseException | None = None
        self.image_error: tuple[int, dict[str, object]] | None = None
        self.fail_image_at: int | None = None
        self.posts_response: dict[str, object] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path == "/youtube/v3/channels":
            self.channels_calls += 1
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": self.channel_id,
                            "snippet": {"title": "Studio", "customUrl": "@studio"},
                        }
                    ]
                },
            )
        if request.method == "POST" and path == "/upload/youtube/v3/posts/image":
            self.image_calls += 1
            if self.image_error is not None:
                status, payload = self.image_error
                return httpx.Response(status, json=payload)
            if self.fail_image_at is not None and len(self.image_uploads) == self.fail_image_at:
                self.fail_image_at = None
                raise httpx.ReadTimeout("timed out mid image upload")
            params = parse_qs(request.url.query.decode())
            token = f"img-tok-{self.image_calls}"
            self.image_uploads.append(
                {
                    "channelId": params.get("channelId", [None])[0],
                    "content_type": request.headers.get("content-type"),
                    "body": request.content,
                    "token": token,
                }
            )
            return httpx.Response(200, json={"imageUploadToken": token})
        if request.method == "POST" and path == "/youtube/v3/posts":
            self.insert_calls += 1
            self.insert_params.append(dict(parse_qs(request.url.query.decode())))
            self.insert_bodies.append(json.loads(request.content.decode()))
            if self.insert_failure is not None:
                raise self.insert_failure
            if self.insert_error is not None:
                status, payload = self.insert_error
                return httpx.Response(status, json=payload)
            body = self.insert_bodies[-1]
            post_id = f"UgkxFake{len(self.posts) + 1:02d}"
            attachments = dict(body.get("attachments") or {})
            post = {
                "id": post_id,
                "snippet": {
                    "channelId": body["snippet"]["channelId"],
                    "textDisplay": body["snippet"]["textDisplay"],
                    "publishedAt": "2026-11-23T12:00:00Z",
                },
                "attachments": attachments,
            }
            self.posts.append(post)
            return httpx.Response(200, json=post)
        if request.method == "GET" and path == "/youtube/v3/posts":
            self.list_calls += 1
            if self.posts_response is not None:
                return httpx.Response(200, json=self.posts_response)
            params = parse_qs(request.url.query.decode())
            wanted = params.get("id", [None])[0]
            channel = params.get("channelId", [None])[0]
            items = [
                post
                for post in self.posts
                if (wanted is None or post["id"] == wanted)
                and (
                    channel is None or dict(post["snippet"])["channelId"] == channel  # type: ignore[index]
                )
            ]
            return httpx.Response(200, json={"items": items})
        return httpx.Response(
            404,
            json={
                "error": {
                    "message": f"unhandled {request.method} {request.url}",
                    "errors": [{"reason": "notFound"}],
                }
            },
        )


def make_publisher(
    settings: Settings, database: Database, fake: FakePostsAPI
) -> YouTubePostPublisher:
    return YouTubePostPublisher(
        settings,
        database,
        client=httpx.Client(transport=httpx.MockTransport(fake), timeout=20.0),
    )


class Recorder:
    def __init__(self) -> None:
        self.marked: list[datetime] = []
        self.saved: list[dict[str, object]] = []


def image_ref(
    position: int,
    *,
    content_type: str = "image/png",
    size_bytes: int = len(IMAGE_BYTES),
    probe: dict[str, object] | None = None,
) -> MediaRef:
    return MediaRef(
        asset_id=position + 1,
        position=position,
        kind="image",
        sha256=f"{position:064x}",
        size_bytes=size_bytes,
        content_type=content_type,
        storage="local",
        storage_key=f"w/1/image{position}.png",
        original_filename=f"image{position}.png",
        probe=probe if probe is not None else {"width": 800, "height": 600},
    )


def make_ctx(
    *,
    post_type: str = "youtube_post",
    caption: str = "The caption",
    image_count: int = 1,
    media: tuple[MediaRef, ...] | None = None,
    channel_id: str = "UCexpected",
    granted_scopes: tuple[str, ...] = (ENGAGE,),
    marked: Recorder | None = None,
    resumable: dict[str, object] | None = None,
    external_id: str | None = None,
    request_sent_at: datetime | None = None,
    open_media_raises: bool = False,
    post_id: int = 1,
    target_id: int = 7,
) -> PublishContext:
    recorder = marked or Recorder()
    images = media if media is not None else tuple(image_ref(i) for i in range(image_count))
    streams = {ref.storage_key: IMAGE_BYTES for ref in images}

    def mark() -> datetime:
        recorder.marked.append(NOW)
        return NOW

    def opener(ref: MediaRef) -> io.BytesIO:
        if open_media_raises:
            raise OSError("the file vanished")
        return io.BytesIO(streams[ref.storage_key])

    return PublishContext(
        workspace_id=1,
        post_id=post_id,
        target_id=target_id,
        platform=Platform.youtube,
        post_type=post_type,
        publisher="youtube_post",
        account=AccountRef(
            id=1,
            platform=Platform.youtube,
            external_account_id=channel_id,
            handle="@studio",
            status="connected",
            granted_scopes=granted_scopes,
            token_ref="tok-1",
        ),
        title="Post title",
        caption=caption,
        settings={},
        media=images,
        planned_at=None,
        payload_hash="b" * 64,
        ai_generated=False,
        external_id=external_id,
        resumable=dict(resumable or {}),
        request_sent_at=request_sent_at,
        clock=lambda: NOW,
        _mark_request_sent=mark,
        _save_resumable=lambda state: recorder.saved.append(dict(state)),
        _open_media=opener,
        _access_token=lambda: TokenSet(
            access_token="yt-access",
            scopes=list(granted_scopes),
            extra={"channel_id": channel_id},
        ),
    )


# --------------------------------------------------------------------- send


def test_send_uploads_each_image_then_inserts_after_the_marker(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakePostsAPI()
    publisher = make_publisher(settings, database, fake)
    recorder = Recorder()
    ctx = make_ctx(image_count=3, marked=recorder)

    result = publisher.send(ctx, NullProgress())

    assert result.status == "published"
    assert result.external_id == "UgkxFake01"
    assert result.permalink == "https://www.youtube.com/post/UgkxFake01"
    # One imageUpload per image, in order, with the channel as a query param
    # and the file's own content type.
    assert fake.image_calls == 3
    assert [upload["channelId"] for upload in fake.image_uploads] == ["UCexpected"] * 3
    assert all(upload["content_type"] == "image/png" for upload in fake.image_uploads)
    assert all(upload["body"] == IMAGE_BYTES for upload in fake.image_uploads)
    # The marker was committed exactly once, before posts.insert.
    assert fake.insert_calls == 1
    assert len(recorder.marked) == 1
    parts = set(fake.insert_params[0]["part"][0].split(","))
    assert {"snippet", "attachments"} <= parts
    body = fake.insert_bodies[0]
    assert body["snippet"] == {"channelId": "UCexpected", "textDisplay": "The caption"}
    tokens = [entry["uploadToken"] for entry in body["attachments"]["imageAttachments"]]
    assert tokens == ["img-tok-1", "img-tok-2", "img-tok-3"]


def test_a_text_only_post_inserts_without_attachments(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakePostsAPI()
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx(image_count=0, caption="just text")

    result = publisher.send(ctx, NullProgress())

    assert result.status == "published"
    assert fake.image_calls == 0
    assert fake.insert_calls == 1
    body = fake.insert_bodies[0]
    assert body["snippet"]["textDisplay"] == "just text"
    assert not body.get("attachments", {}).get("imageAttachments")


def test_a_failed_image_upload_never_reaches_the_marker(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakePostsAPI()
    fake.image_error = (
        400,
        {"error": {"message": "image rejected", "errors": [{"reason": "badImage"}]}},
    )
    publisher = make_publisher(settings, database, fake)
    recorder = Recorder()
    ctx = make_ctx(image_count=2, marked=recorder)

    with pytest.raises(PublishError) as caught:
        publisher.send(ctx, NullProgress())

    assert caught.value.delivered is False
    assert recorder.marked == []
    assert fake.insert_calls == 0


def test_a_retry_before_the_marker_uploads_every_image_again(tmp_path: Path) -> None:
    """`imageUploadToken`s are never reused: a re-run re-uploads all images."""
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakePostsAPI()
    publisher = make_publisher(settings, database, fake)
    recorder = Recorder()
    ctx = make_ctx(image_count=2, marked=recorder)

    fake.fail_image_at = 1  # the second imageUpload times out (pre-marker)
    with pytest.raises(ProviderError):
        publisher.send(ctx, NullProgress())
    assert fake.image_calls == 2
    assert fake.insert_calls == 0
    assert recorder.marked == []

    result = publisher.send(make_ctx(image_count=2, marked=recorder), NullProgress())

    assert result.status == "published"
    # Every image was uploaded again: tokens from the crashed run were dropped.
    assert fake.image_calls == 4
    assert fake.insert_calls == 1
    tokens = [
        entry["uploadToken"] for entry in fake.insert_bodies[0]["attachments"]["imageAttachments"]
    ]
    assert tokens == ["img-tok-3", "img-tok-4"]


def test_a_read_timeout_on_the_insert_is_uncertain_not_retried(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakePostsAPI()
    fake.insert_failure = httpx.ReadTimeout("no response after the insert")
    publisher = make_publisher(settings, database, fake)
    recorder = Recorder()
    ctx = make_ctx(marked=recorder)

    with pytest.raises(ProviderError) as caught:
        publisher.send(ctx, NullProgress())

    assert len(recorder.marked) == 1, "the marker precedes posts.insert"
    failure = classify_send_failure(caught.value, request_sent=True)
    assert failure.state == "uncertain"


def test_a_wrong_channel_token_is_refused_before_any_write(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakePostsAPI(channel_id="UCsomeone-else")
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx(channel_id="UCexpected")

    with pytest.raises(PublishError) as caught:
        publisher.send(ctx, NullProgress())

    assert caught.value.code == "wrong_channel"
    assert caught.value.final is True and caught.value.delivered is False
    assert fake.image_calls == 0
    assert fake.insert_calls == 0


@pytest.mark.parametrize("stage", ["image", "insert"])
def test_quota_errors_retry_after_pacific_midnight(tmp_path: Path, stage: str) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakePostsAPI()
    error = (
        403,
        {
            "error": {
                "code": 403,
                "message": "post quota exhausted",
                "errors": [{"reason": "quotaExceeded", "message": "post quota exhausted"}],
            }
        },
    )
    if stage == "image":
        fake.image_error = error
    else:
        fake.insert_error = error
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx()

    with pytest.raises(PublishRetryLater) as caught:
        publisher.send(ctx, NullProgress())

    retry_at = caught.value.retry_after
    assert retry_at is not None
    local = retry_at.astimezone(ZoneInfo("America/Los_Angeles"))
    assert (local.hour, local.minute) == (0, 0)


def test_a_401_on_insert_is_auth_revoked(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakePostsAPI()
    fake.insert_error = (
        401,
        {"error": {"message": "bad credentials", "errors": [{"reason": "authError"}]}},
    )
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx()

    with pytest.raises(PublishError) as caught:
        publisher.send(ctx, NullProgress())

    assert caught.value.code == "auth_revoked"
    assert caught.value.final is True


def test_a_server_error_on_insert_retries_shortly(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakePostsAPI()
    fake.insert_error = (
        500,
        {"error": {"message": "backend exploded", "errors": [{"reason": "backendError"}]}},
    )
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx()

    with pytest.raises(PublishRetryLater) as caught:
        publisher.send(ctx, NullProgress())

    assert caught.value.retry_after == NOW + timedelta(minutes=5)


def test_an_unnamed_refusal_is_final(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakePostsAPI()
    fake.insert_error = (
        400,
        {"error": {"message": "the post is not allowed", "errors": [{"reason": "badPost"}]}},
    )
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx()

    with pytest.raises(PublishError) as caught:
        publisher.send(ctx, NullProgress())

    assert caught.value.final is True and caught.value.delivered is False


# ----------------------------------------------------------------- preflight


def test_preflight_needs_the_pilot_flag_and_the_scope(tmp_path: Path) -> None:
    settings = live_settings(tmp_path, youtube_posts_api_access=False)
    database = Database(settings)
    publisher = make_publisher(settings, database, FakePostsAPI())

    issues = publisher.preflight(make_ctx())
    assert any(issue.code == "posts_api_not_enabled" for issue in issues)

    piloted = make_publisher(live_settings(tmp_path), database, FakePostsAPI())
    missing = piloted.preflight(make_ctx(granted_scopes=()))
    assert any(issue.code == "missing_scope" for issue in missing)


def test_preflight_enforces_the_image_rules(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    publisher = make_publisher(settings, database, FakePostsAPI())

    wrong_type = publisher.preflight(
        make_ctx(
            media=(
                MediaRef(
                    asset_id=1,
                    position=0,
                    kind="video",
                    sha256="a" * 64,
                    size_bytes=10,
                    content_type="video/mp4",
                    storage="local",
                    storage_key="w/1/clip.mp4",
                ),
            )
        )
    )
    assert any(issue.code == "media_kind_not_supported" for issue in wrong_type)

    tiff = publisher.preflight(make_ctx(media=(image_ref(0, content_type="image/tiff"),)))
    assert any(issue.code == "image_format" for issue in tiff)

    too_big = publisher.preflight(make_ctx(media=(image_ref(0, size_bytes=17 * 1024 * 1024),)))
    assert any(issue.code == "file_too_large" for issue in too_big)

    too_wide = publisher.preflight(
        make_ctx(media=(image_ref(0, probe={"width": 4000, "height": 100}),))
    )
    assert any(issue.code == "aspect_ratio" for issue in too_wide)

    empty = publisher.preflight(make_ctx(image_count=0, caption="   "))
    assert any(issue.code == "post_empty" for issue in empty)

    too_many = publisher.preflight(make_ctx(image_count=11))
    assert any(issue.code == "media_count" for issue in too_many)


def test_preflight_checks_the_file_can_be_read(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    publisher = make_publisher(settings, database, FakePostsAPI())

    issues = publisher.preflight(make_ctx(open_media_raises=True))
    assert any(issue.code == "file_unreadable" for issue in issues)


def test_preflight_accepts_text_only_and_valid_images(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    publisher = make_publisher(settings, database, FakePostsAPI())

    assert publisher.preflight(make_ctx()) == []
    assert publisher.preflight(make_ctx(image_count=0, caption="text")) == []


def test_preflight_refuses_other_post_types(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    publisher = make_publisher(settings, database, FakePostsAPI())

    issues = publisher.preflight(make_ctx(post_type="youtube_video"))
    assert issues and all(issue.severity == "error" for issue in issues)


# ------------------------------------------------------------- verify + poll


def test_verify_reads_the_post_back_and_compares(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakePostsAPI()
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx(image_count=2)
    result = publisher.send(ctx, NullProgress())

    verified = publisher.verify(make_ctx(image_count=2, external_id=result.external_id))
    assert verified.verified is True
    assert verified.permalink == "https://www.youtube.com/post/UgkxFake01"

    wrong_text = publisher.verify(
        make_ctx(image_count=2, caption="different", external_id=result.external_id)
    )
    assert wrong_text.verified is False
    assert any("text" in item for item in wrong_text.mismatches)

    wrong_count = publisher.verify(make_ctx(image_count=3, external_id=result.external_id))
    assert wrong_count.verified is False
    assert any("image" in item for item in wrong_count.mismatches)


def test_verify_without_the_post_is_unknown_not_false(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    publisher = make_publisher(settings, database, FakePostsAPI())

    result = publisher.verify(make_ctx(external_id="UgkxMissing"))
    assert result.verified is None


def test_poll_reports_published_or_still_missing(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakePostsAPI()
    publisher = make_publisher(settings, database, fake)
    result = publisher.send(make_ctx(), NullProgress())

    found = publisher.poll(make_ctx(external_id=result.external_id))
    assert found.status == "published"
    assert found.permalink == "https://www.youtube.com/post/UgkxFake01"

    missing = publisher.poll(make_ctx(external_id="UgkxMissing"))
    assert missing.status == "processing"


# ------------------------------------------------------------------ reconcile


def test_reconcile_finds_the_post_by_its_id(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakePostsAPI()
    publisher = make_publisher(settings, database, fake)
    sent = publisher.send(make_ctx(), NullProgress())

    result = publisher.reconcile(make_ctx(external_id=sent.external_id, request_sent_at=NOW))
    assert result.status == "published"
    assert result.external_id == "UgkxFake01"


def test_reconcile_matches_the_newest_posts_by_text_and_image_count(
    tmp_path: Path,
) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakePostsAPI()
    publisher = make_publisher(settings, database, fake)
    # The post exists on the channel; the attempt lost the id (crash after
    # the marker, before the response).
    publisher.send(make_ctx(image_count=2, caption="matching text"), NullProgress())

    found = publisher.reconcile(
        make_ctx(image_count=2, caption="matching text", request_sent_at=NOW)
    )
    assert found.status == "published"
    assert found.external_id == "UgkxFake01"

    nothing = publisher.reconcile(
        make_ctx(image_count=1, caption="something else", request_sent_at=NOW)
    )
    assert nothing.status == "absent"


def test_reconcile_is_uncertain_when_two_recent_posts_match(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakePostsAPI()
    publisher = make_publisher(settings, database, fake)
    publisher.send(make_ctx(caption="same"), NullProgress())
    publisher.send(make_ctx(caption="same"), NullProgress())

    result = publisher.reconcile(make_ctx(caption="same", request_sent_at=NOW))
    assert result.status == "uncertain"
    assert len(result.candidates) == 2


def test_reconcile_ignores_posts_older_than_the_attempt_window(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakePostsAPI()
    publisher = make_publisher(settings, database, fake)
    publisher.send(make_ctx(caption="old identical post"), NullProgress())
    # Same text published well before the attempt's window: not a match.
    fake.posts[0]["snippet"]["publishedAt"] = "2026-11-23T10:00:00Z"  # 2 h ago

    result = publisher.reconcile(make_ctx(caption="old identical post", request_sent_at=NOW))
    assert result.status == "absent"


# ----------------------------------------------------------------- rate guard


def test_the_per_channel_daily_guard_blocks_at_five(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    publisher = make_publisher(settings, database, FakePostsAPI())
    guard = publisher.rate_guards[0]

    def decide(sent: int) -> object:
        return guard(
            RateGuardContext(
                workspace_id=1,
                account_id=1,
                platform=Platform.youtube,
                post_type="youtube_post",
                at=NOW,
                count_sent=lambda _since, _scope: sent,
            )
        )

    assert decide(4).allowed
    blocked = decide(5)
    assert not blocked.allowed
    local = blocked.retry_at.astimezone(ZoneInfo("America/Los_Angeles"))
    assert (local.hour, local.minute) == (0, 0)
