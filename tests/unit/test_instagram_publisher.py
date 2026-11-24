"""`instagram_direct` publisher (spec 08 section 3, "Send").

Two steps: `POST /<IG_ID>/media` makes containers (up to 30 minutes before
`planned_at`, via `prepare`), `POST /<IG_ID>/media_publish` with the
container id publishes it — that call is the point of no return, marked
right before it. Error 9007/subcode 2207027 is retried with the same
`creation_id` every 10 s for up to 2 minutes, then `uncertain`. Media must
be on a public URL at publish time; local media is staged on R2 and the
copy is deleted once the post reaches a terminal state.
"""

from __future__ import annotations

import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest

from runway.config import Settings
from runway.db.base import Database
from runway.mediastore.store import UploadPlan
from runway.platforms.base import TokenSet
from runway.platforms.capabilities import Platform
from runway.platforms.instagram import API_VERSION
from runway.platforms.instagram_publisher import (
    InstagramDirectPublisher,
    instagram_publish_guards,
)
from runway.platforms.publisher import (
    AccountRef,
    MediaRef,
    PublishContext,
    PublishError,
    PublishRefused,
    PublishRetryLater,
    classify_send_failure,
)
from runway.platforms.publisher_registry import (
    LIVE_PUBLISHER_NAMES,
    build_publisher_registry,
)
from runway.posts.types import TargetPostType

IG = "17841400923817"
GRAPH = f"https://graph.instagram.com/{API_VERSION}"
NOW = datetime(2026, 12, 1, 15, 0, tzinfo=UTC)


# ------------------------------------------------------------------ fixtures


class FakeGraph:
    """graph.instagram.com endpoints the publisher may call, over MockTransport."""

    def __init__(self) -> None:
        self.next_container = 0
        self.containers: dict[str, dict[str, object]] = {}
        self.media_counter = 0
        self.media_rows: dict[str, dict[str, object]] = {}
        self.media_order: list[str] = []
        self.quota_usage = 0
        self.quota_total: int | None = 50
        self.me_id = IG
        self.publish_outcomes: list[object] = []
        self.media_post_responses: list[httpx.Response] = []
        self.requests: list[tuple[str, str, dict[str, object]]] = []
        self.marker_at: list[int] = []
        self._marker_count = lambda: 0

    def watch_marker(self, count) -> None:
        self._marker_count = count

    def new_media(self) -> str:
        self.media_counter += 1
        media_id = f"media{self.media_counter}"
        self.media_rows[media_id] = {
            "id": media_id,
            "permalink": f"https://www.instagram.com/p/{media_id}/",
            "media_type": "REELS",
            "timestamp": "2026-12-01T15:00:10+0000",
        }
        self.media_order.insert(0, media_id)
        return media_id

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.lstrip("/")
        if path.startswith(API_VERSION + "/"):
            path = path[len(API_VERSION) + 1 :]
        method = request.method
        body = parse_qs(request.content.decode()) if request.content else {}
        self.requests.append((method, path, {k: v[0] for k, v in body.items()}))
        # Every graph call must authenticate like the real API: a request
        # without the held token's Bearer header is an OAuth failure (190).
        if request.headers.get("Authorization") != "Bearer ig-tok":
            return httpx.Response(
                401,
                json={
                    "error": {
                        "code": 190,
                        "message": "Invalid OAuth access token - cannot parse access token",
                    }
                },
            )
        if path == "me":
            return httpx.Response(200, json={"id": self.me_id, "user_id": self.me_id})
        if path.endswith("/content_publishing_limit"):
            # The real endpoint wraps the record in `data` and returns `config`
            # only when the fields parameter asks for it.
            entry: dict[str, object] = {"quota_usage": self.quota_usage}
            fields = str(request.url.params.get("fields", ""))
            if "config" in fields and self.quota_total is not None:
                entry["config"] = {"quota_total": self.quota_total}
            return httpx.Response(200, json={"data": [entry]})
        if path.endswith("/media") and method == "POST":
            if self.media_post_responses:
                return self.media_post_responses.pop(0)
            self.next_container += 1
            container_id = f"c{self.next_container}"
            self.containers[container_id] = {
                "status_code": "FINISHED",
                "params": {k: v[0] for k, v in body.items()},
            }
            return httpx.Response(200, json={"id": container_id})
        if path.endswith("/media") and method == "GET":
            return httpx.Response(
                200,
                json={
                    "data": [
                        self.media_rows[mid] for mid in self.media_order if mid in self.media_rows
                    ]
                },
            )
        if path.endswith("/media_publish") and method == "POST":
            self.marker_at.append(self._marker_count())
            if self.publish_outcomes:
                outcome = self.publish_outcomes.pop(0)
                if isinstance(outcome, httpx.Response):
                    return outcome
                return httpx.Response(200, json={"id": outcome})
            return httpx.Response(200, json={"id": self.new_media()})
        if path in self.containers:
            container = self.containers[path]
            return httpx.Response(
                200,
                json={
                    "id": path,
                    "status_code": container["status_code"],
                    "status": container.get("status", ""),
                },
            )
        if path in self.media_rows:
            return httpx.Response(200, json=self.media_rows[path])
        return httpx.Response(404, json={"error": {"code": 100, "message": f"no {path}"}})


def error_9007() -> httpx.Response:
    return httpx.Response(
        400,
        json={
            "error": {
                "code": 9007,
                "error_subcode": 2207027,
                "message": "Media ID is not available",
            }
        },
    )


class FakeR2:
    """The R2 surface the publisher stages media through (PUTs go to its URLs)."""

    def __init__(self) -> None:
        self.plans: list[UploadPlan] = []
        self.completed: list[str] = []
        self.deleted: list[str] = []
        self.signed: list[str] = []

    def begin_upload(self, key, size, content_type, *, sha256, workspace_id):
        plan = UploadPlan(
            store="r2",
            mode="direct",
            key=key,
            upload_id=f"u-{len(self.plans)}",
            size=size,
            sha256=sha256,
            content_type=content_type,
            part_size=max(size, 8 * 1024 * 1024),
            created_at=NOW,
        )
        self.plans.append(plan)
        return plan

    def part_upload_urls(self, plan, part_numbers=None, *, ttl_seconds=3600):
        numbers = list(plan.part_numbers()) if part_numbers is None else list(part_numbers)
        return {n: f"https://r2.test/put/{n}?sig" for n in numbers}

    def complete_upload(self, plan, parts=()):
        self.completed.append(plan.key)

    def signed_get_url(self, key, ttl_seconds, *, workspace_id):
        self.signed.append(key)
        return f"https://r2.test/get/{key}?sig"

    def put_object(self, key, data, *, workspace_id, content_type):
        pass

    def object_size(self, key, *, workspace_id):
        return None

    def signed_download_url(self, key, ttl_seconds, *, workspace_id, filename):
        return None

    def delete(self, key, *, workspace_id):
        self.deleted.append(key)


def _media(
    kind: str = "video",
    *,
    storage: str = "r2",
    key: str = "w/1/7/aaaabbbbccccdddd.mp4",
    size: int = 12345,
    position: int = 0,
) -> MediaRef:
    return MediaRef(
        asset_id=7 + position,
        position=position,
        kind=kind,
        sha256="a" * 64,
        size_bytes=size,
        content_type="video/mp4" if kind == "video" else "image/jpeg",
        storage=storage,
        storage_key=key,
        original_filename=f"item{position}.{kind}",
    )


def _context(
    post_type: str = "instagram_reel",
    *,
    media: tuple[MediaRef, ...] | None = None,
    settings: dict | None = None,
    resumable: dict | None = None,
    caption: str = "a caption",
    signed: dict[str, str] | None = None,
    user_id: str = IG,
    account_status: str = "connected",
    scopes: tuple[str, ...] = ("instagram_business_content_publish",),
    token: TokenSet | None = None,
) -> tuple[PublishContext, dict[str, int], list[dict]]:
    marks = {"count": 0}
    saves: list[dict] = []

    def mark() -> datetime:
        marks["count"] += 1
        return datetime.now(UTC)

    signed_map = signed if signed is not None else {}

    return (
        PublishContext(
            workspace_id=1,
            post_id=1,
            target_id=7,
            platform=Platform.instagram,
            post_type=post_type,
            publisher="instagram_direct",
            account=AccountRef(
                id=3,
                platform=Platform.instagram,
                external_account_id=IG,
                handle="northwindstudio",
                status=account_status,
                granted_scopes=scopes,
                token_ref="tok-ref",
                account_type="BUSINESS",
            ),
            title="T",
            caption=caption,
            settings=settings or {},
            media=media if media is not None else (_media(),),
            planned_at=NOW + timedelta(minutes=20),
            payload_hash="b" * 64,
            ai_generated=False,
            resumable=dict(resumable or {}),
            clock=lambda: NOW,
            _mark_request_sent=mark,
            _save_resumable=lambda s: saves.append(dict(s)),
            _open_media=lambda m: io.BytesIO(b"\x00" * m.size_bytes),
            _signed_media_url=lambda m, ttl: signed_map.get(m.storage_key),
            _access_token=lambda: (
                token or TokenSet(access_token="ig-tok", extra={"user_id": user_id})
            ),
        ),
        marks,
        saves,
    )


def _read_context(**kwargs: object) -> tuple[PublishContext, dict[str, int], list[dict]]:
    """A poll/verify/reconcile context: persisting hooks refuse (no attempt)."""
    ctx, marks, saves = _context(**kwargs)

    def refuse(*_args: object, **_kw: object) -> None:
        raise RuntimeError("save_resumable is only available to the worker while sending")

    ctx._save_resumable = refuse
    return ctx, marks, saves


class FakeClock:
    def __init__(self, start: datetime = NOW) -> None:
        self.now = start
        self.slept = 0.0

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept += seconds
        self.now += timedelta(seconds=seconds)


def _publisher(
    graph: FakeGraph,
    *,
    r2: FakeR2 | None = None,
    clock: FakeClock | None = None,
    settings: Settings | None = None,
) -> InstagramDirectPublisher:
    clock = clock or FakeClock()
    return InstagramDirectPublisher(
        settings or Settings(data_dir=Path("unused")),
        Database(Settings(data_dir=Path("unused2"))),
        client=httpx.Client(transport=httpx.MockTransport(graph.handler)),
        upload_client=httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, headers={"etag": '"e1"'})
            )
        ),
        r2=r2,
        clock=clock,
        sleep=clock.sleep,
    )


# ------------------------------------------------------------------ preflight


def test_preflight_flags_fields_media_scope_and_host(tmp_path: Path) -> None:
    publisher = _publisher(FakeGraph())
    ctx, _marks, _saves = _context(
        caption="x" * 2300,
        media=(_media("image"),),  # a reel needs video
        scopes=(),
        signed={},
    )
    codes = {issue.code for issue in publisher.preflight(ctx)}
    assert "caption_too_long" in codes
    assert "media_kind" in codes
    assert "scope_missing" in codes
    assert "media_host" in codes
    publisher2 = _publisher(FakeGraph(), r2=FakeR2())
    ctx2, _m, _s = _context(
        media=(_media("video", storage="local", key="w/1/7/cccccccccccccccc.mp4"),),
        signed={},
    )
    codes2 = {issue.code for issue in publisher2.preflight(ctx2)}
    assert "media_host" not in codes2


def test_preflight_clean_for_a_valid_reel() -> None:
    publisher = _publisher(FakeGraph())
    ctx, _m, _s = _context(signed={"w/1/7/aaaabbbbccccdddd.mp4": "https://sig.test/x"})
    assert publisher.preflight(ctx) == []


def test_supports_exactly_the_instagram_post_types() -> None:
    publisher = _publisher(FakeGraph())
    assert {member.value for member in TargetPostType if publisher.supports(member.value)} == {
        "instagram_reel",
        "instagram_image",
        "instagram_carousel",
        "instagram_story",
    }


def test_registered_in_the_live_build(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    registry = build_publisher_registry(settings, Database(settings))
    assert registry.names() == LIVE_PUBLISHER_NAMES
    assert "instagram_direct" in registry.names()
    assert "instagram_direct" in LIVE_PUBLISHER_NAMES
    assert registry.require("instagram_direct").platform is Platform.instagram


# ------------------------------------------------------------------- prepare


def test_prepare_creates_a_reel_container_and_polls_finished() -> None:
    graph = FakeGraph()
    publisher = _publisher(graph)
    ctx, _m, _s = _context(
        signed={"w/1/7/aaaabbbbccccdddd.mp4": "https://sig.test/v"},
        settings={"share_to_feed": True, "is_ai_generated": False},
        caption="cap #one",
    )
    state = publisher.prepare(ctx)
    created = [r for r in graph.requests if r[1] == f"{IG}/media"]
    assert len(created) == 1
    params = created[0][2]
    assert params["media_type"] == "REELS"
    assert params["video_url"] == "https://sig.test/v"
    assert params["caption"] == "cap #one"
    assert params["share_to_feed"] == "true"
    assert params["is_ai_generated"] == "false"
    assert state["containers"] == [{"id": "c1", "position": 0, "role": "main"}]
    assert state["r2_keys"] == []


def test_prepare_creates_carousel_children_then_parent() -> None:
    graph = FakeGraph()
    publisher = _publisher(graph)
    media = (
        _media("image", key="w/1/7/a1a1a1a1a1a1a1a1.jpg", position=0),
        _media("video", key="w/1/7/b2b2b2b2b2b2b2b2.mp4", position=1),
    )
    signed = {m.storage_key: f"https://sig.test/{m.position}" for m in media}
    ctx, _m, _s = _context(
        post_type="instagram_carousel", media=media, signed=signed, caption="multi"
    )
    state = publisher.prepare(ctx)
    posts = [r for r in graph.requests if r[1] == f"{IG}/media" and r[0] == "POST"]
    assert len(posts) == 3
    assert posts[0][2]["is_carousel_item"] == "true"
    assert posts[0][2]["image_url"] == "https://sig.test/0"
    assert "caption" not in posts[0][2]
    assert posts[1][2]["video_url"] == "https://sig.test/1"
    parent = posts[2][2]
    assert parent["media_type"] == "CAROUSEL"
    assert parent["children"] == "c1,c2"
    assert parent["caption"] == "multi"
    assert [c["id"] for c in state["containers"]] == ["c1", "c2", "c3"]
    assert state["containers"][2]["role"] == "parent"


def test_prepare_stages_local_media_on_r2() -> None:
    graph = FakeGraph()
    r2 = FakeR2()
    publisher = _publisher(graph, r2=r2)
    ctx, _m, _s = _context(
        media=(_media("video", storage="local", key="w/1/7/dddddddddddddddd.mp4"),),
        signed={},
    )
    state = publisher.prepare(ctx)
    assert len(r2.plans) == 1
    key = r2.plans[0].key
    assert key.startswith("w/1/igpub7p0/")
    params = [r for r in graph.requests if r[1] == f"{IG}/media"][0][2]
    assert params["video_url"] == f"https://r2.test/get/{key}?sig"
    assert state["r2_keys"] == [key]


# ---------------------------------------------------------------------- send


def test_send_publishes_marked_and_cleans_the_r2_copy() -> None:
    graph = FakeGraph()
    r2 = FakeR2()
    publisher = _publisher(graph, r2=r2)
    key = "w/1/igpub7p0/" + "e" * 16 + ".mp4"
    ctx, marks, _s = _context(
        resumable={
            "containers": [{"id": "c9", "position": 0, "role": "main"}],
            "r2_keys": [key],
        }
    )
    graph.watch_marker(lambda: marks["count"])
    result = publisher.send(ctx, None)
    publishes = [r for r in graph.requests if r[1] == f"{IG}/media_publish"]
    assert len(publishes) == 1
    assert publishes[0][2]["creation_id"] == "c9"
    assert graph.marker_at == [1]  # request_sent marked exactly before publish
    assert result.status == "published"
    assert result.external_id == "media1"
    assert r2.deleted == [key]
    assert marks["count"] == 1


def test_send_creates_containers_when_nothing_was_prepared() -> None:
    graph = FakeGraph()
    publisher = _publisher(graph)
    ctx, marks, _s = _context(signed={"w/1/7/aaaabbbbccccdddd.mp4": "https://sig.test/v"})
    result = publisher.send(ctx, None)
    assert result.status == "published"
    created = [r for r in graph.requests if r[1] == f"{IG}/media" and r[0] == "POST"]
    assert len(created) == 1
    publish = [r for r in graph.requests if r[1] == f"{IG}/media_publish"][0]
    assert publish[2]["creation_id"] == "c1"
    assert marks["count"] == 1


def test_send_retries_media_publish_on_9007_with_the_same_creation_id() -> None:
    graph = FakeGraph()
    graph.publish_outcomes = [error_9007(), "media7"]
    clock = FakeClock()
    publisher = _publisher(graph, clock=clock)
    ctx, _m, _s = _context(resumable={"containers": [{"id": "c1", "position": 0, "role": "main"}]})
    result = publisher.send(ctx, None)
    publishes = [r for r in graph.requests if r[1] == f"{IG}/media_publish"]
    assert len(publishes) == 2
    assert {r[2]["creation_id"] for r in publishes} == {"c1"}
    assert result.status == "published"
    assert result.external_id == "media7"
    assert clock.slept == pytest.approx(10)


def test_send_takes_the_media_id_when_the_container_is_already_published() -> None:
    graph = FakeGraph()
    graph.containers["c1"] = {"status_code": "PUBLISHED", "params": {}}
    graph.next_container = 1
    published_id = graph.new_media()
    graph.publish_outcomes = [error_9007()]
    publisher = _publisher(graph)
    ctx, _m, _s = _context(resumable={"containers": [{"id": "c1", "position": 0, "role": "main"}]})
    result = publisher.send(ctx, None)
    publishes = [r for r in graph.requests if r[1] == f"{IG}/media_publish"]
    assert len(publishes) == 1
    assert result.status == "published"
    assert result.external_id == published_id


def test_send_is_uncertain_when_9007_outlives_the_budget() -> None:
    graph = FakeGraph()
    graph.containers["c1"] = {"status_code": "IN_PROGRESS", "params": {}}
    graph.next_container = 1
    graph.publish_outcomes = [error_9007()] * 20
    clock = FakeClock()
    publisher = _publisher(graph, clock=clock)
    ctx, marks, _s = _context(
        resumable={"containers": [{"id": "c1", "position": 0, "role": "main"}]}
    )
    with pytest.raises(PublishError) as caught:
        publisher.send(ctx, None)
    failure = classify_send_failure(caught.value, request_sent=marks["count"] > 0)
    assert failure.state == "uncertain"
    assert clock.slept >= 120
    assert marks["count"] == 1


def test_send_recreates_an_expired_container_once() -> None:
    graph = FakeGraph()
    graph.containers["c1"] = {"status_code": "EXPIRED", "params": {}}
    graph.next_container = 1
    publisher = _publisher(graph)
    ctx, _m, _s = _context(
        signed={"w/1/7/aaaabbbbccccdddd.mp4": "https://sig.test/v"},
        resumable={"containers": [{"id": "c1", "position": 0, "role": "main"}]},
    )
    result = publisher.send(ctx, None)
    posts = [r for r in graph.requests if r[1] == f"{IG}/media" and r[0] == "POST"]
    assert len(posts) == 1  # the recreation, not the first one
    publish = [r for r in graph.requests if r[1] == f"{IG}/media_publish"][0]
    assert publish[2]["creation_id"] == "c2"
    assert result.status == "published"


def test_send_recreates_expired_carousel_children_then_the_parent() -> None:
    graph = FakeGraph()
    graph.containers["c3"] = {"status_code": "EXPIRED", "params": {}}
    graph.next_container = 3
    publisher = _publisher(graph)
    media = (
        _media("image", key="w/1/7/a1a1a1a1a1a1a1a1.jpg", position=0),
        _media("image", key="w/1/7/b2b2b2b2b2b2b2b2.jpg", position=1),
    )
    signed = {m.storage_key: f"https://sig.test/{m.position}" for m in media}
    ctx, _m, _s = _context(
        post_type="instagram_carousel",
        media=media,
        signed=signed,
        resumable={
            "containers": [
                {"id": "c1", "position": 0, "role": "child"},
                {"id": "c2", "position": 1, "role": "child"},
                {"id": "c3", "position": -1, "role": "parent"},
            ]
        },
    )
    result = publisher.send(ctx, None)
    posts = [r for r in graph.requests if r[1] == f"{IG}/media" and r[0] == "POST"]
    # The expired children are recreated first (the expired ids cannot be
    # reused), then the parent over the fresh child ids.
    assert len(posts) == 3
    assert posts[0][2]["is_carousel_item"] == "true"
    assert posts[0][2]["image_url"] == "https://sig.test/0"
    assert posts[1][2]["is_carousel_item"] == "true"
    assert posts[1][2]["image_url"] == "https://sig.test/1"
    assert posts[2][2]["media_type"] == "CAROUSEL"
    assert posts[2][2]["children"] == "c4,c5"
    publish = [r for r in graph.requests if r[1] == f"{IG}/media_publish"][0]
    assert publish[2]["creation_id"] == "c6"
    assert result.status == "published"


def test_send_recreates_expired_containers_only_once_per_target() -> None:
    graph = FakeGraph()
    graph.containers["c2"] = {"status_code": "EXPIRED", "params": {}}
    graph.next_container = 2
    publisher = _publisher(graph)
    ctx, _m, _s = _context(
        signed={"w/1/7/aaaabbbbccccdddd.mp4": "https://sig.test/v"},
        resumable={
            "containers": [{"id": "c2", "position": 0, "role": "main"}],
            # A first recreation already happened for this target.
            "recreated": True,
        },
    )
    with pytest.raises(PublishRefused) as caught:
        publisher.send(ctx, None)
    assert caught.value.code == "container_expired"
    assert "release again" in caught.value.detail
    assert not [r for r in graph.requests if r[1] == f"{IG}/media" and r[0] == "POST"]
    assert not [r for r in graph.requests if r[1] == f"{IG}/media_publish"]


def test_send_treats_a_legacy_recreated_list_as_already_recreated() -> None:
    graph = FakeGraph()
    graph.containers["c2"] = {"status_code": "EXPIRED", "params": {}}
    graph.next_container = 2
    publisher = _publisher(graph)
    ctx, _m, _s = _context(
        signed={"w/1/7/aaaabbbbccccdddd.mp4": "https://sig.test/v"},
        resumable={
            "containers": [{"id": "c2", "position": 0, "role": "main"}],
            # Older builds stored the recreated container ids in a list.
            "recreated": ["c1"],
        },
    )
    with pytest.raises(PublishRefused) as caught:
        publisher.send(ctx, None)
    assert caught.value.code == "container_expired"
    assert not [r for r in graph.requests if r[1] == f"{IG}/media" and r[0] == "POST"]


def test_send_refuses_when_the_publishing_quota_is_exhausted() -> None:
    graph = FakeGraph()
    graph.quota_usage = 50
    publisher = _publisher(graph)
    ctx, marks, _s = _context(
        resumable={"containers": [{"id": "c1", "position": 0, "role": "main"}]}
    )
    with pytest.raises(PublishRetryLater):
        publisher.send(ctx, None)
    assert marks["count"] == 0
    assert not [r for r in graph.requests if r[1] == f"{IG}/media_publish"]


def test_send_uses_the_reported_quota_headroom_above_fifty() -> None:
    graph = FakeGraph()
    graph.quota_usage = 60
    graph.quota_total = 100  # the reported cap beats the documented 50
    publisher = _publisher(graph)
    ctx, _m, _s = _context(resumable={"containers": [{"id": "c1", "position": 0, "role": "main"}]})
    result = publisher.send(ctx, None)
    assert result.status == "published"


def test_send_refuses_at_the_reported_quota_total() -> None:
    graph = FakeGraph()
    graph.quota_usage = 25
    graph.quota_total = 25
    publisher = _publisher(graph)
    ctx, marks, _s = _context(
        resumable={"containers": [{"id": "c1", "position": 0, "role": "main"}]}
    )
    with pytest.raises(PublishRetryLater):
        publisher.send(ctx, None)
    assert marks["count"] == 0


def test_send_falls_back_to_the_static_cap_when_no_quota_is_reported() -> None:
    graph = FakeGraph()
    graph.quota_usage = 50
    graph.quota_total = None  # config absent: the documented 50/day applies
    publisher = _publisher(graph)
    ctx, marks, _s = _context(
        resumable={"containers": [{"id": "c1", "position": 0, "role": "main"}]}
    )
    with pytest.raises(PublishRetryLater):
        publisher.send(ctx, None)
    assert marks["count"] == 0


def test_send_refuses_when_the_token_belongs_to_another_account() -> None:
    graph = FakeGraph()
    graph.me_id = "999"
    publisher = _publisher(graph)
    ctx, marks, _s = _context(user_id="999")
    with pytest.raises(PublishRefused) as caught:
        publisher.send(ctx, None)
    assert caught.value.code == "account_mismatch"
    assert marks["count"] == 0


def test_send_fails_final_when_a_container_is_in_error() -> None:
    graph = FakeGraph()
    graph.containers["c1"] = {
        "status_code": "ERROR",
        "status": "codec rejected",
        "params": {},
    }
    graph.next_container = 1
    publisher = _publisher(graph)
    ctx, marks, _s = _context(
        resumable={"containers": [{"id": "c1", "position": 0, "role": "main"}]}
    )
    with pytest.raises(PublishRefused) as caught:
        publisher.send(ctx, None)
    assert "codec rejected" in caught.value.detail
    assert marks["count"] == 0
    assert not [r for r in graph.requests if r[1] == f"{IG}/media_publish"]


def test_send_deletes_the_r2_copy_when_the_container_is_in_error() -> None:
    graph = FakeGraph()
    graph.containers["c1"] = {
        "status_code": "ERROR",
        "status": "codec rejected",
        "params": {},
    }
    graph.next_container = 1
    r2 = FakeR2()
    publisher = _publisher(graph, r2=r2)
    key = "w/1/igpub7p0/" + "e" * 16 + ".mp4"
    ctx, _m, _s = _context(
        resumable={
            "containers": [{"id": "c1", "position": 0, "role": "main"}],
            "r2_keys": [key],
        }
    )
    with pytest.raises(PublishRefused) as caught:
        publisher.send(ctx, None)
    assert caught.value.code == "container_error"
    assert r2.deleted == [key]


def test_send_deletes_the_r2_copy_when_recreation_is_exhausted() -> None:
    graph = FakeGraph()
    graph.containers["c2"] = {"status_code": "EXPIRED", "params": {}}
    graph.next_container = 2
    r2 = FakeR2()
    publisher = _publisher(graph, r2=r2)
    key = "w/1/igpub7p0/" + "e" * 16 + ".mp4"
    ctx, _m, _s = _context(
        resumable={
            "containers": [{"id": "c2", "position": 0, "role": "main"}],
            "r2_keys": [key],
            "recreated": True,
        }
    )
    with pytest.raises(PublishRefused) as caught:
        publisher.send(ctx, None)
    assert caught.value.code == "container_expired"
    assert r2.deleted == [key]


def test_send_keeps_the_r2_copy_on_a_retryable_failure() -> None:
    graph = FakeGraph()
    graph.media_post_responses = [
        httpx.Response(429, json={"error": {"code": 4, "message": "rate limited"}})
    ]
    r2 = FakeR2()
    publisher = _publisher(graph, r2=r2)
    ctx, _m, _s = _context(
        media=(_media("video", storage="local", key="w/1/7/dddddddddddddddd.mp4"),),
        signed={},
    )
    with pytest.raises(PublishRetryLater):
        publisher.send(ctx, None)
    # The staged copy survives a retryable failure: the retry reuses it.
    assert r2.completed and r2.deleted == []


def test_story_uses_the_stories_media_type_and_video_url() -> None:
    graph = FakeGraph()
    publisher = _publisher(graph)
    ctx, _m, _s = _context(
        post_type="instagram_story",
        signed={"w/1/7/aaaabbbbccccdddd.mp4": "https://sig.test/s"},
    )
    publisher.send(ctx, None)
    params = [r for r in graph.requests if r[1] == f"{IG}/media" and r[0] == "POST"][0][2]
    assert params["media_type"] == "STORIES"
    assert params["video_url"] == "https://sig.test/s"
    assert "caption" not in params


def test_reel_settings_map_to_container_parameters() -> None:
    graph = FakeGraph()
    publisher = _publisher(graph)
    ctx, _m, _s = _context(
        settings={
            "share_to_feed": False,
            "thumb_offset_ms": 1200,
            "audio_name": "original audio",
            "collaborators": ["friend1", "friend2"],
            "trial": "MANUAL",
            "is_ai_generated": True,
        },
        signed={"w/1/7/aaaabbbbccccdddd.mp4": "https://sig.test/v"},
    )
    publisher.send(ctx, None)
    params = [r for r in graph.requests if r[1] == f"{IG}/media" and r[0] == "POST"][0][2]
    assert params["share_to_feed"] == "false"
    assert params["thumb_offset"] == "1200"
    assert params["audio_name"] == "original audio"
    assert json.loads(params["collaborators"]) == ["friend1", "friend2"]
    assert json.loads(params["trial_params"]) == {"graduation_strategy": "MANUAL"}
    assert params["is_ai_generated"] == "true"


def test_image_settings_map_to_container_parameters() -> None:
    graph = FakeGraph()
    publisher = _publisher(graph)
    ctx, _m, _s = _context(
        post_type="instagram_image",
        media=(_media("image", key="w/1/7/a1a1a1a1a1a1a1a1.jpg"),),
        settings={
            "alt_text": "alt",
            "user_tags": [{"username": "pal", "x": 0.5, "y": 0.25}],
            "location_id": "123",
            "collaborators": ["pal"],
            "is_ai_generated": False,
        },
        signed={"w/1/7/a1a1a1a1a1a1a1a1.jpg": "https://sig.test/i"},
    )
    publisher.send(ctx, None)
    params = [r for r in graph.requests if r[1] == f"{IG}/media" and r[0] == "POST"][0][2]
    assert params["image_url"] == "https://sig.test/i"
    assert params["alt_text"] == "alt"
    assert json.loads(params["user_tags"]) == [{"username": "pal", "x": 0.5, "y": 0.25}]
    assert params["location_id"] == "123"


# --------------------------------------------------------------------- poll


def test_poll_reports_the_container_status() -> None:
    graph = FakeGraph()
    graph.containers["c1"] = {"status_code": "IN_PROGRESS", "params": {}}
    graph.next_container = 1
    publisher = _publisher(graph)
    ctx, _m, _s = _context(resumable={"containers": [{"id": "c1", "position": 0, "role": "main"}]})
    result = publisher.poll(ctx)
    assert result.status == "processing"
    graph.containers["c1"]["status_code"] = "ERROR"
    failed = publisher.poll(ctx)
    assert failed.status == "failed"
    assert failed.retryable is False


def test_poll_reports_published_with_the_media_id() -> None:
    graph = FakeGraph()
    graph.containers["c1"] = {"status_code": "PUBLISHED", "params": {}}
    graph.next_container = 1
    media_id = graph.new_media()
    publisher = _publisher(graph)
    ctx, _m, _s = _context(resumable={"containers": [{"id": "c1", "position": 0, "role": "main"}]})
    result = publisher.poll(ctx)
    assert result.status == "published"
    assert result.external_id == media_id


def test_poll_deletes_the_r2_copy_on_a_terminal_status() -> None:
    for status in ("PUBLISHED", "EXPIRED", "ERROR"):
        graph = FakeGraph()
        graph.containers["c1"] = {"status_code": status, "params": {}}
        graph.next_container = 1
        if status == "PUBLISHED":
            graph.new_media()
        r2 = FakeR2()
        publisher = _publisher(graph, r2=r2)
        key = "w/1/igpub7p0/" + "e" * 16 + ".mp4"
        ctx, _m, _s = _read_context(
            resumable={
                "containers": [{"id": "c1", "position": 0, "role": "main"}],
                "r2_keys": [key],
            }
        )
        result = publisher.poll(ctx)
        assert result.status == ("published" if status == "PUBLISHED" else "failed")
        assert r2.deleted == [key]


# -------------------------------------------------------------------- verify


def test_verify_reads_the_media_and_returns_the_permalink() -> None:
    graph = FakeGraph()
    media_id = graph.new_media()
    publisher = _publisher(graph)
    ctx, _m, _s = _context()
    ctx.external_id = media_id
    result = publisher.verify(ctx)
    assert result.verified is True
    assert result.permalink == f"https://www.instagram.com/p/{media_id}/"


def test_verify_waits_when_the_media_cannot_be_read() -> None:
    graph = FakeGraph()
    publisher = _publisher(graph)
    ctx, _m, _s = _context()
    ctx.external_id = "missing"
    result = publisher.verify(ctx)
    assert result.verified is None


# ------------------------------------------------------------------ reconcile


def test_reconcile_takes_the_media_id_when_the_container_published() -> None:
    graph = FakeGraph()
    graph.containers["c1"] = {"status_code": "PUBLISHED", "params": {}}
    graph.next_container = 1
    media_id = graph.new_media()
    publisher = _publisher(graph)
    ctx, _m, _s = _context(resumable={"containers": [{"id": "c1", "position": 0, "role": "main"}]})
    result = publisher.reconcile(ctx)
    assert result.status == "published"
    assert result.external_id == media_id
    assert "permalink" in (result.detail or "") or result.permalink is not None


def test_reconcile_says_absent_when_the_container_was_never_published() -> None:
    graph = FakeGraph()
    graph.containers["c1"] = {"status_code": "FINISHED", "params": {}}
    graph.next_container = 1
    publisher = _publisher(graph)
    ctx, _m, _s = _context(resumable={"containers": [{"id": "c1", "position": 0, "role": "main"}]})
    result = publisher.reconcile(ctx)
    assert result.status == "absent"


def test_reconcile_says_absent_for_expired_or_failed_containers() -> None:
    graph = FakeGraph()
    for status in ("EXPIRED", "ERROR"):
        graph.containers[f"c_{status}"] = {"status_code": status, "params": {}}
    publisher = _publisher(graph)
    for status in ("EXPIRED", "ERROR"):
        ctx, _m, _s = _context(
            resumable={"containers": [{"id": f"c_{status}", "position": 0, "role": "main"}]}
        )
        assert publisher.reconcile(ctx).status == "absent"


def test_reconcile_is_uncertain_without_a_container_record() -> None:
    graph = FakeGraph()
    publisher = _publisher(graph)
    ctx, _m, _s = _context(resumable={})
    result = publisher.reconcile(ctx)
    assert result.status == "uncertain"
    assert result.checked


def test_reconcile_deletes_the_r2_copy_on_a_read_context() -> None:
    for status in ("FINISHED", "EXPIRED", "ERROR"):
        graph = FakeGraph()
        graph.containers["c1"] = {"status_code": status, "params": {}}
        graph.next_container = 1
        r2 = FakeR2()
        publisher = _publisher(graph, r2=r2)
        key = "w/1/igpub7p0/" + "e" * 16 + ".mp4"
        # A reconcile context has no open attempt: save_resumable refuses.
        ctx, _m, _s = _read_context(
            resumable={
                "containers": [{"id": "c1", "position": 0, "role": "main"}],
                "r2_keys": [key],
            }
        )
        result = publisher.reconcile(ctx)
        assert result.status == "absent"
        assert r2.deleted == [key]


# ------------------------------------------------------------- rate guards


def test_rate_guard_blocks_at_fifty_posts_per_rolling_day() -> None:
    from runway.platforms.publisher import RateGuardContext

    guard = instagram_publish_guards()[0]
    context = RateGuardContext(
        workspace_id=1,
        account_id=3,
        platform=Platform.instagram,
        post_type="instagram_reel",
        at=NOW,
        count_sent=lambda since, scope: 49,
    )
    assert guard(context).allowed
    full = RateGuardContext(
        workspace_id=1,
        account_id=3,
        platform=Platform.instagram,
        post_type="instagram_reel",
        at=NOW,
        count_sent=lambda since, scope: 50,
    )
    decision = guard(full)
    assert not decision.allowed
    assert "50" in decision.detail
