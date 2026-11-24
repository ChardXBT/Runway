"""The `youtube_direct` adapter against a fake YouTube (httpx.MockTransport).

Spec 08 section 2 "Send" for `youtube_video` / `youtube_short` (youtube-a
package 2): resumable session with `part=snippet,status` and
`paidProductPlacementDetails`, 8 MB chunks with the confirmed offset persisted
after each one, the point-of-no-return marker before the final byte range,
`processing` polling, `thumbnails.set` as a warning-only step, read-back
verification, session/upload matching in `reconcile`, the named-error map, the
Pacific-day upload counter and the cached categories route.

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
from fastapi.testclient import TestClient

from runway.api.app import create_app
from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import ConnectedAccount, MediaAssetV2
from runway.platforms.base import ProviderError, TokenSet
from runway.platforms.capabilities import Platform
from runway.platforms.publish_capability import YOUTUBE_UPLOAD_SCOPE
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
from runway.platforms.registry import ProviderRegistry
from runway.platforms.youtube import YouTubeProvider
from runway.platforms.youtube_direct import YouTubeDirectPublisher

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
PLANNED = NOW + timedelta(hours=2)
# Two 8 MiB chunks plus a tail: three PUT requests on a fresh upload.
VIDEO_BYTES = b"\x11" * (17 * 1024 * 1024 + 123)
THUMB_BYTES = b"\xff\xd8\xff" + b"\x00" * 2048
UPLOAD_SCOPE = YOUTUBE_UPLOAD_SCOPE


def migrated(settings: Settings) -> Settings:
    initialize_database(settings).engine.dispose()
    return settings


def live_settings(tmp_path: Path, **overrides: object) -> Settings:
    return migrated(
        Settings(
            data_dir=tmp_path / "data",
            platform_provider="live",
            youtube_client_id="yt-client",
            youtube_client_secret="yt-secret",
            **overrides,  # type: ignore[arg-type]
        )
    )


def youtube_settings(**overrides: object) -> dict[str, object]:
    """A complete, explicit `youtube_video`/`youtube_short` settings payload."""
    values: dict[str, object] = {
        "title": "Launch day",
        "description": "The full description",
        "tags": ["launch"],
        "category_id": "22",
        "privacy": "unlisted",
        "made_for_kids": False,
        "has_paid_product_placement": False,
        "contains_synthetic_media": False,
        "notify_subscribers": True,
        "embeddable": None,
        "license": None,
        "schedule_mode": "held",
        "thumbnail_asset_id": None,
    }
    values.update(overrides)
    return values


def _parse_range(header: str) -> tuple[int, int, int]:
    prefix, total = header.split("/")
    first, last = prefix.removeprefix("bytes ").split("-")
    return int(first), int(last), int(total)


class FakeYouTube:
    """A stateful fake of the upload + data endpoints the adapter calls."""

    def __init__(self, *, channel_id: str = "UCexpected", uploads_playlist: str = "UUup") -> None:
        self.channel_id = channel_id
        self.uploads_playlist = uploads_playlist
        self.video_id = "vid-abc"
        self.session_uri = "https://upload.youtube.com/upload/youtube/v3/videos?upload_id=sess-1"
        self.session_bodies: list[dict[str, object]] = []
        self.session_params: list[dict[str, list[str]]] = []
        self.session_calls = 0
        self.chunk_puts: list[tuple[str, int]] = []
        self.status_queries = 0
        self.received = 0
        self.session_error: tuple[int, dict[str, object]] | None = None
        self.session_query_response: httpx.Response | None = None
        self.fail_chunk_at: int | None = None
        self.chunk_failure: BaseException = httpx.ReadTimeout("timed out")
        self.videos_response: dict[str, object] = {"items": []}
        self.videos_calls = 0
        self.playlist_items: list[dict[str, object]] = []
        self.thumbnail_status = 204
        self.thumbnail_calls = 0
        self.categories_calls = 0
        self.channels_calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/upload/youtube/v3/videos":
            self.session_calls += 1
            self.session_params.append(dict(parse_qs(request.url.query.decode())))
            self.session_bodies.append(json.loads(request.content.decode()))
            if self.session_error is not None:
                status, payload = self.session_error
                return httpx.Response(status, json=payload)
            return httpx.Response(200, headers={"Location": self.session_uri})
        if request.method == "PUT" and "upload_id=sess" in str(request.url):
            content_range = request.headers["content-range"]
            if "*" in content_range:
                self.status_queries += 1
                if self.session_query_response is not None:
                    return self.session_query_response
                return httpx.Response(308, headers={"Range": f"bytes=0-{self.received - 1}"})
            if self.fail_chunk_at is not None and len(self.chunk_puts) == self.fail_chunk_at:
                self.fail_chunk_at = None
                raise self.chunk_failure
            first, last, total = _parse_range(content_range)
            body = request.content
            assert len(body) == last - first + 1
            assert first == self.received, f"gap: server has {self.received}, chunk starts {first}"
            self.chunk_puts.append((content_range, len(body)))
            self.received = last + 1
            if last < total - 1:
                return httpx.Response(308, headers={"Range": f"bytes=0-{last}"})
            return httpx.Response(
                201,
                json={"id": self.video_id, "status": {"uploadStatus": "uploaded"}},
            )
        if request.method == "GET" and path == "/youtube/v3/channels":
            self.channels_calls += 1
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": self.channel_id,
                            "snippet": {"title": "Studio", "customUrl": "@studio"},
                            "statistics": {},
                            "contentDetails": {
                                "relatedPlaylists": {"uploads": self.uploads_playlist}
                            },
                        }
                    ]
                },
            )
        if request.method == "GET" and path == "/youtube/v3/videos":
            self.videos_calls += 1
            return httpx.Response(200, json=self.videos_response)
        if request.method == "GET" and path == "/youtube/v3/playlistItems":
            return httpx.Response(200, json={"items": self.playlist_items})
        if request.method == "POST" and path == "/upload/youtube/v3/thumbnails/set":
            self.thumbnail_calls += 1
            if self.thumbnail_status >= 400:
                return httpx.Response(
                    self.thumbnail_status,
                    json={
                        "error": {
                            "code": self.thumbnail_status,
                            "message": "channel cannot set thumbnails",
                            "errors": [{"reason": "forbidden"}],
                        }
                    },
                )
            return httpx.Response(self.thumbnail_status)
        if request.method == "GET" and path == "/youtube/v3/videoCategories":
            self.categories_calls += 1
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "22",
                            "snippet": {"title": "People & Blogs", "assignable": True},
                        },
                        {"id": "10", "snippet": {"title": "Music", "assignable": True}},
                    ]
                },
            )
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
    settings: Settings, database: Database, fake: FakeYouTube
) -> YouTubeDirectPublisher:
    return YouTubeDirectPublisher(
        settings,
        database,
        client=httpx.Client(transport=httpx.MockTransport(fake), timeout=20.0),
    )


class Recorder:
    def __init__(self) -> None:
        self.marked: list[datetime] = []
        self.saved: list[dict[str, object]] = []


def make_ctx(
    *,
    settings: dict[str, object] | None = None,
    post_type: str = "youtube_video",
    planned_at: datetime | None = None,
    resumable: dict[str, object] | None = None,
    external_id: str | None = None,
    channel_id: str = "UCexpected",
    granted_scopes: tuple[str, ...] = (UPLOAD_SCOPE,),
    media_bytes: bytes = VIDEO_BYTES,
    media_map: dict[str, bytes] | None = None,
    probe: dict[str, object] | None = None,
    marked: Recorder | None = None,
    request_sent_at: datetime | None = None,
) -> PublishContext:
    recorder = marked or Recorder()
    streams: dict[str, bytes] = {"w/1/clip.mp4": media_bytes}
    if media_map:
        streams.update(media_map)

    def mark() -> datetime:
        recorder.marked.append(NOW)
        return NOW

    return PublishContext(
        workspace_id=1,
        post_id=1,
        target_id=7,
        platform=Platform.youtube,
        post_type=post_type,
        publisher="youtube_direct",
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
        caption="The caption",
        settings=settings if settings is not None else youtube_settings(),
        media=(
            MediaRef(
                asset_id=1,
                position=0,
                kind="video",
                sha256="a" * 64,
                size_bytes=len(media_bytes),
                content_type="video/mp4",
                storage="local",
                storage_key="w/1/clip.mp4",
                original_filename="clip.mp4",
                probe=probe
                if probe is not None
                else {"duration_ms": 60_000, "width": 1920, "height": 1080},
            ),
        ),
        planned_at=planned_at,
        payload_hash="b" * 64,
        ai_generated=False,
        external_id=external_id,
        resumable=dict(resumable or {}),
        request_sent_at=request_sent_at,
        clock=lambda: NOW,
        _mark_request_sent=mark,
        _save_resumable=lambda state: recorder.saved.append(dict(state)),
        _open_media=lambda ref: io.BytesIO(streams[ref.storage_key]),
        _access_token=lambda: TokenSet(
            access_token="yt-access",
            scopes=[UPLOAD_SCOPE],
            extra={"channel_id": channel_id, "uploads_playlist": "UUup"},
        ),
    )


# --------------------------------------------------------------------- send


def test_held_send_uploads_in_chunks_and_marks_before_the_final_range(
    tmp_path: Path,
) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakeYouTube()
    publisher = make_publisher(settings, database, fake)
    recorder = Recorder()
    ctx = make_ctx(marked=recorder)

    result = publisher.send(ctx, NullProgress())

    assert result.status == "processing"
    assert result.external_id == "vid-abc"
    assert result.permalink == "https://www.youtube.com/watch?v=vid-abc"
    # One resumable session, part includes status and paid placement.
    assert fake.session_calls == 1
    parts = set(fake.session_params[0]["part"][0].split(","))
    assert {"snippet", "status"} <= parts
    assert "paidProductPlacementDetails" in parts
    body = fake.session_bodies[0]
    snippet = body["snippet"]
    status = body["status"]
    assert snippet["title"] == "Launch day"
    assert snippet["categoryId"] == "22"
    assert snippet["tags"] == ["launch"]
    # Held mode sends the chosen privacy; no publishAt.
    assert status["privacyStatus"] == "unlisted"
    assert "publishAt" not in status
    assert status["selfDeclaredMadeForKids"] is False
    assert status["containsSyntheticMedia"] is False
    assert body["paidProductPlacementDetails"] == {"hasPaidProductPlacement": False}
    # 17 MiB + 123 B: two full 8 MiB chunks and a tail, in order, contiguous.
    assert len(fake.chunk_puts) == 3
    ranges = [_parse_range(header) for header, _ in fake.chunk_puts]
    assert ranges[0][:2] == (0, 8 * 1024 * 1024 - 1)
    assert ranges[-1][1] == len(VIDEO_BYTES) - 1
    # The confirmed offset was persisted after each chunk (session start, then
    # 8 MiB and 16 MiB confirmed).
    offsets = [saved["offset"] for saved in recorder.saved if "offset" in saved]
    assert offsets == [0, 8 * 1024 * 1024, 16 * 1024 * 1024]
    assert any(saved.get("session_uri") for saved in recorder.saved)
    # The marker was committed exactly once, before the final PUT.
    assert len(recorder.marked) == 1


def test_native_scheduling_sends_private_with_publish_at(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakeYouTube()
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx(
        settings=youtube_settings(privacy="public", schedule_mode="native"),
        planned_at=PLANNED,
    )

    publisher.send(ctx, NullProgress())

    status = fake.session_bodies[0]["status"]
    assert status["privacyStatus"] == "private"
    published_at = datetime.fromisoformat(str(status["publishAt"]).replace("Z", "+00:00"))
    assert published_at == PLANNED


def test_resume_after_a_crash_completes_the_same_upload(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakeYouTube()
    publisher = make_publisher(settings, database, fake)
    recorder = Recorder()
    ctx = make_ctx(marked=recorder)

    # Crash on the second chunk PUT (a read timeout: retryable pre-marker).
    fake.fail_chunk_at = 1
    with pytest.raises(ProviderError):
        publisher.send(ctx, NullProgress())
    assert fake.session_calls == 1
    saved = {key: value for state in recorder.saved for key, value in state.items()}
    assert saved["session_uri"] == fake.session_uri
    assert saved["offset"] == 8 * 1024 * 1024
    assert recorder.marked == []

    # The retry queries the session, resumes at the confirmed offset and only
    # the final chunk is sent; the marker fires once, before that final PUT.
    resumed = make_ctx(marked=recorder, resumable=saved)
    result = publisher.send(resumed, NullProgress())

    assert result.status == "processing" and result.external_id == "vid-abc"
    assert fake.session_calls == 1, "no second session may be created"
    assert fake.status_queries == 1
    assert len(fake.chunk_puts) == 3  # 1 earlier + 2 now
    assert _parse_range(fake.chunk_puts[-1][0])[0] == 16 * 1024 * 1024
    assert len(recorder.marked) == 1


def test_a_read_timeout_on_the_final_chunk_is_uncertain_not_retried(
    tmp_path: Path,
) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakeYouTube()
    publisher = make_publisher(settings, database, fake)
    recorder = Recorder()
    ctx = make_ctx(marked=recorder)
    # Fail the last chunk (index 2 of three).
    fake.fail_chunk_at = 2

    with pytest.raises(ProviderError) as caught:
        publisher.send(ctx, NullProgress())

    assert len(recorder.marked) == 1, "the marker precedes the final byte range"
    failure = classify_send_failure(caught.value, request_sent=True)
    assert failure.state == "uncertain"


def test_a_wrong_channel_token_is_refused_before_any_write(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakeYouTube(channel_id="UCsomeone-else")
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx(channel_id="UCexpected")

    with pytest.raises(PublishError) as caught:
        publisher.send(ctx, NullProgress())

    assert caught.value.code == "wrong_channel"
    assert caught.value.final is True and caught.value.delivered is False
    assert fake.session_calls == 0
    assert fake.chunk_puts == []


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("uploadLimitExceeded", "refused"),
        ("quotaExceeded", "retry"),
        ("invalidPublishAt", "refused"),
        ("forbiddenPrivacySetting", "refused"),
        ("invalidTitle", "refused"),
        ("invalidDescription", "refused"),
        ("invalidTags", "refused"),
        ("invalidCategoryId", "refused"),
    ],
)
def test_named_errors_map_to_final_or_retryable(tmp_path: Path, reason: str, expected: str) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakeYouTube()
    fake.session_error = (
        400,
        {
            "error": {
                "code": 400,
                "message": "metadata rejected",
                "errors": [{"reason": reason, "message": "metadata rejected"}],
            }
        },
    )
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx()

    with pytest.raises(PublishError) as caught:
        publisher.send(ctx, NullProgress())

    if expected == "retry":
        assert isinstance(caught.value, PublishRetryLater)
        assert caught.value.retry_after is not None
        # Retry after Pacific midnight: the retry time is midnight in
        # America/Los_Angeles, not a fixed offset.
        local_retry = caught.value.retry_after.astimezone(ZoneInfo("America/Los_Angeles"))
        assert (local_retry.hour, local_retry.minute) == (0, 0)
    else:
        # A final refusal: the platform answered and nothing was published.
        assert caught.value.final is True and caught.value.delivered is False


def test_preflight_blocks_missing_declarations_and_bad_files(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    publisher = make_publisher(settings, database, FakeYouTube())

    missing = youtube_settings(privacy=None, made_for_kids=None, has_paid_product_placement=None)
    issues = publisher.preflight(make_ctx(settings=missing))
    fields = {issue.field for issue in issues if issue.severity == "error"}
    assert {"privacy", "made_for_kids", "has_paid_product_placement"} <= fields

    missing_scope = publisher.preflight(make_ctx(granted_scopes=()))
    assert any(issue.code == "missing_scope" for issue in missing_scope)


def test_native_preflight_needs_a_future_time_and_public_privacy(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    publisher = make_publisher(settings, database, FakeYouTube())

    # publishAt would make the video public at the planned time; unlisted +
    # native cannot be sent without violating the chosen privacy.
    unlisted_native = publisher.preflight(
        make_ctx(
            settings=youtube_settings(privacy="unlisted", schedule_mode="native"),
            planned_at=PLANNED,
        )
    )
    assert any(issue.severity == "error" for issue in unlisted_native)

    past = publisher.preflight(
        make_ctx(
            settings=youtube_settings(privacy="public", schedule_mode="native"),
            planned_at=NOW - timedelta(minutes=1),
        )
    )
    assert any(issue.severity == "error" for issue in past)


def test_youtube_short_flags_the_heuristic(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    publisher = make_publisher(settings, database, FakeYouTube())
    # A landscape 4-minute video is unlikely to be treated as a Short.
    issues = publisher.preflight(
        make_ctx(
            post_type="youtube_short",
            probe={"duration_ms": 240_000, "width": 1920, "height": 1080},
        )
    )
    warnings = [issue for issue in issues if issue.severity == "warning"]
    assert warnings and any("short" in issue.message.lower() for issue in warnings)


# ---------------------------------------------------------------------- poll


def _processed_video(video_id: str = "vid-abc") -> dict[str, object]:
    return {
        "id": video_id,
        "status": {"uploadStatus": "processed", "privacyStatus": "unlisted"},
        "processingDetails": {"processingStatus": "succeeded"},
    }


def test_poll_reports_processing_then_published(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakeYouTube()
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx(external_id="vid-abc")

    fake.videos_response = {"items": [{"id": "vid-abc", "status": {"uploadStatus": "uploaded"}}]}
    result = publisher.poll(ctx)
    assert result.status == "processing"
    assert result.next_poll_seconds is not None

    fake.videos_response = {"items": [_processed_video()]}
    result = publisher.poll(ctx)
    assert result.status == "published"
    assert result.external_id == "vid-abc"


def test_poll_maps_failed_and_rejected_to_final(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakeYouTube()
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx(external_id="vid-abc")

    fake.videos_response = {
        "items": [
            {
                "id": "vid-abc",
                "status": {"uploadStatus": "failed", "failureReason": "codec"},
            }
        ]
    }
    result = publisher.poll(ctx)
    assert result.status == "failed" and result.retryable is False
    assert result.error_code == "codec"

    fake.videos_response = {
        "items": [
            {
                "id": "vid-abc",
                "status": {"uploadStatus": "rejected", "rejectionReason": "copyright"},
            }
        ]
    }
    result = publisher.poll(ctx)
    assert result.status == "failed" and result.retryable is False


def _thumbnail_asset(database: Database, asset_id: int = 5) -> None:
    with database.session() as session:
        session.add(
            MediaAssetV2(
                id=asset_id,
                workspace_id=1,
                kind="image",
                original_filename="thumb.jpg",
                content_type="image/jpeg",
                size_bytes=len(THUMB_BYTES),
                sha256="c" * 64,
                storage="local",
                storage_key="w/1/thumb.jpg",
                status="ready",
                probe_json="{}",
            )
        )


def test_a_403_on_the_thumbnail_is_a_warning_not_a_failure(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    _thumbnail_asset(database)
    fake = FakeYouTube()
    fake.thumbnail_status = 403
    fake.videos_response = {"items": [_processed_video()]}
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx(
        settings=youtube_settings(thumbnail_asset_id=5),
        external_id="vid-abc",
        media_map={"w/1/thumb.jpg": THUMB_BYTES},
    )

    result = publisher.poll(ctx)

    assert result.status == "published"
    assert fake.thumbnail_calls == 1
    warnings = result.response.get("warnings") or []
    assert warnings and "thumbnail" in str(warnings[0]).lower()


# -------------------------------------------------------------------- verify


def test_verify_compares_title_privacy_publish_at_and_paid_placement(
    tmp_path: Path,
) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakeYouTube()
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx(external_id="vid-abc")

    fake.videos_response = {
        "items": [
            {
                "id": "vid-abc",
                "snippet": {"title": "Launch day"},
                "status": {"privacyStatus": "unlisted"},
                "paidProductPlacementDetails": {"hasPaidProductPlacement": False},
            }
        ]
    }
    result = publisher.verify(ctx)
    assert result.verified is True

    fake.videos_response = {
        "items": [
            {
                "id": "vid-abc",
                "snippet": {"title": "Launch day"},
                "status": {"privacyStatus": "public"},
                "paidProductPlacementDetails": {"hasPaidProductPlacement": True},
            }
        ]
    }
    result = publisher.verify(ctx)
    assert result.verified is False
    assert result.mismatches


def test_verify_native_video_still_private_until_publish_at(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakeYouTube()
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx(
        settings=youtube_settings(privacy="public", schedule_mode="native"),
        planned_at=PLANNED,
        external_id="vid-abc",
    )
    fake.videos_response = {
        "items": [
            {
                "id": "vid-abc",
                "snippet": {"title": "Launch day"},
                "status": {
                    "privacyStatus": "private",
                    "publishAt": PLANNED.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                },
                "paidProductPlacementDetails": {"hasPaidProductPlacement": False},
            }
        ]
    }
    assert publisher.verify(ctx).verified is True


# ----------------------------------------------------------------- reconcile


def _uploaded_item(
    video_id: str,
    *,
    title: str = "Launch day",
    duration: str = "PT1M",
    published_at: datetime | None = None,
) -> dict[str, object]:
    return {
        "id": video_id,
        "snippet": {
            "title": title,
            "publishedAt": (published_at or NOW - timedelta(minutes=2)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            ),
        },
        "contentDetails": {"duration": duration},
        "status": {"uploadStatus": "processed", "privacyStatus": "unlisted"},
    }


def test_reconcile_308_session_means_safe_to_resume(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakeYouTube()
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx(
        resumable={"session_uri": fake.session_uri, "offset": 8 * 1024 * 1024},
        request_sent_at=NOW - timedelta(minutes=5),
    )

    result = publisher.reconcile(ctx)

    assert result.status == "absent"
    assert fake.status_queries == 1


def test_reconcile_a_completed_session_returns_the_video(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakeYouTube()
    fake.session_query_response = httpx.Response(
        201, json={"id": "vid-abc", "status": {"uploadStatus": "uploaded"}}
    )
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx(
        resumable={"session_uri": fake.session_uri, "offset": len(VIDEO_BYTES)},
        request_sent_at=NOW - timedelta(minutes=5),
    )

    result = publisher.reconcile(ctx)

    assert result.status == "published"
    assert result.external_id == "vid-abc"


def test_reconcile_an_expired_session_matches_newest_uploads(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakeYouTube()
    fake.session_query_response = httpx.Response(404, json={})
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx(
        resumable={"session_uri": fake.session_uri},
        request_sent_at=NOW - timedelta(minutes=5),
        external_id=None,
    )
    fake.playlist_items = [
        {"contentDetails": {"videoId": "match-1"}},
        {"contentDetails": {"videoId": "old-1"}},
    ]
    fake.videos_response = {
        "items": [
            _uploaded_item("match-1"),
            _uploaded_item("old-1", published_at=NOW - timedelta(days=7)),
        ]
    }

    result = publisher.reconcile(ctx)

    assert result.status == "published"
    assert result.external_id == "match-1"
    assert result.permalink == "https://www.youtube.com/watch?v=match-1"


def test_reconcile_no_match_is_absent_and_several_is_uncertain(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    fake = FakeYouTube()
    fake.session_query_response = httpx.Response(404, json={})
    publisher = make_publisher(settings, database, fake)
    ctx = make_ctx(
        resumable={"session_uri": fake.session_uri},
        request_sent_at=NOW - timedelta(minutes=5),
    )

    fake.playlist_items = [{"contentDetails": {"videoId": "other"}}]
    fake.videos_response = {"items": [_uploaded_item("other", title="Different title")]}
    result = publisher.reconcile(ctx)
    assert result.status == "absent"

    fake.playlist_items = [
        {"contentDetails": {"videoId": "a"}},
        {"contentDetails": {"videoId": "b"}},
    ]
    fake.videos_response = {"items": [_uploaded_item("a"), _uploaded_item("b")]}
    result = publisher.reconcile(ctx)
    assert result.status == "uncertain"
    assert len(result.candidates) == 2


# --------------------------------------------------------------- rate guard


def test_the_upload_counter_is_per_pacific_day_and_dst_safe(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    publisher = make_publisher(settings, database, FakeYouTube())
    guard = publisher.rate_guards[0]
    seen_since: list[datetime] = []

    def count(since: datetime, scope: str) -> int:
        seen_since.append(since)
        return 100

    # Spring forward: 2026-03-08, 09:00 UTC is still 01:00 PST; the Pacific
    # day began at 08:00 UTC (PST is UTC-8).
    at = datetime(2026, 3, 8, 9, 0, tzinfo=UTC)
    ctx = RateGuardContext(
        workspace_id=1,
        account_id=1,
        platform=Platform.youtube,
        post_type="youtube_video",
        at=at,
        count_sent=count,
    )
    decision = guard(ctx)
    assert not decision.allowed
    assert seen_since[-1] == datetime(2026, 3, 8, 8, 0, tzinfo=UTC)
    # Retry is the next Pacific midnight (PDT by then, UTC-7).
    assert decision.retry_at == datetime(2026, 3, 9, 7, 0, tzinfo=UTC)

    # Fall back: 2026-11-01, 10:00 UTC is 03:00 PDT; the day began at 07:00 UTC.
    at = datetime(2026, 11, 1, 10, 0, tzinfo=UTC)
    decision = guard(
        RateGuardContext(
            workspace_id=1,
            account_id=1,
            platform=Platform.youtube,
            post_type="youtube_video",
            at=at,
            count_sent=count,
        )
    )
    assert seen_since[-1] == datetime(2026, 11, 1, 7, 0, tzinfo=UTC)
    assert not decision.allowed

    # Room left: the 99th upload of the day is allowed; scope is the whole
    # Google Cloud project ("platform"), not one account.
    decision = guard(
        RateGuardContext(
            workspace_id=1,
            account_id=1,
            platform=Platform.youtube,
            post_type="youtube_video",
            at=at,
            count_sent=lambda since, scope: 99 if scope == "platform" else 0,
        )
    )
    assert decision.allowed


# ------------------------------------------------------------ categories API


def _add_account(database: Database) -> int:
    with database.session() as session:
        account = ConnectedAccount(
            platform="youtube",
            external_account_id="UCexpected",
            handle="@studio",
            status="connected",
            granted_scopes_json=json.dumps([UPLOAD_SCOPE]),
            token_ref="tok-1",
            metadata_json="{}",
        )
        session.add(account)
        session.flush()
        return account.id


def _patched_provider(
    monkeypatch: pytest.MonkeyPatch, settings: Settings, fake: FakeYouTube
) -> YouTubeProvider:
    provider = YouTubeProvider(
        settings, httpx.Client(transport=httpx.MockTransport(fake), timeout=20.0)
    )
    original = ProviderRegistry.provider

    def patched(self: ProviderRegistry, platform: Platform):
        if platform is Platform.youtube:
            return provider
        return original(self, platform)

    monkeypatch.setattr(ProviderRegistry, "provider", patched)
    return provider


def test_categories_route_lists_categories_and_caches_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    account_id = _add_account(database)
    fake = FakeYouTube()
    _patched_provider(monkeypatch, settings, fake)
    from runway.platforms.tokens import TokenStore

    TokenStore(settings.resolved_data_dir / "secrets").store(
        "tok-1",
        TokenSet(
            access_token="yt-access",
            scopes=[UPLOAD_SCOPE],
            extra={"channel_id": "UCexpected", "uploads_playlist": "UUup"},
        ),
    )

    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as web:
        first = web.get(f"/api/v1/accounts/{account_id}/youtube/categories")
        assert first.status_code == 200, first.text
        items = first.json()["items"]
        assert {item["id"] for item in items} == {"22", "10"}
        second = web.get(f"/api/v1/accounts/{account_id}/youtube/categories")
        assert second.status_code == 200
        assert second.json()["items"] == items

    assert fake.categories_calls == 1, "the second call must be served from cache"


def test_categories_route_rejects_non_youtube_accounts(tmp_path: Path) -> None:
    settings = live_settings(tmp_path)
    database = Database(settings)
    with database.session() as session:
        account = ConnectedAccount(
            platform="instagram",
            external_account_id="ig-1",
            status="connected",
            granted_scopes_json="[]",
            metadata_json="{}",
        )
        session.add(account)
        session.flush()
        account_id = account.id
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as web:
        response = web.get(f"/api/v1/accounts/{account_id}/youtube/categories")
        assert response.status_code == 422
