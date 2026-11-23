"""`/api/v1/media` and the probe job (spec 02 sections 3-5, 8), plus the proposal bridge.

A fake prober answers from recorded ffprobe output chosen by the file's magic
bytes, the way ffprobe types a file by content. One test runs the real
ffprobe when it is installed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from runway.api.app import create_app
from runway.auth.principal import ALL_SCOPES, Principal, get_principal
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import (
    CandidateImage,
    ConnectedAccount,
    EventRecord,
    MediaAsset,
    MediaAssetV2,
    Proposal,
    PublishJob,
    Workspace,
)
from runway.jobs.queue import JobQueue
from runway.jobs.worker import Worker
from runway.mediastore import ProbeResult, parse_ffprobe
from runway.mediastore.store import PartReceipt, UploadPlan, UploadProgress
from runway.posts.jobs import PROBE_MEDIA_JOB
from runway.posts.media_service import MediaService

BASE = "http://127.0.0.1:8000"
FIXTURES = Path(__file__).resolve().parents[1] / "unit" / "fixtures" / "ffprobe"
CHUNK = 256 * 1024
JPEG_MAGIC = b"\xff\xd8\xff\xe0"
YOUTUBE = {
    "title": "Launch",
    "category_id": "22",
    "privacy": "unlisted",
    "made_for_kids": False,
    "has_paid_product_placement": False,
    "schedule_mode": "held",
}


def _fixture(name: str, size: int) -> ProbeResult:
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return parse_ffprobe(payload, size_bytes=size)


class FakeProber:
    """Types the file by its bytes: JPEG magic is an image, `WEBM` a VP9 file, else H.264."""

    def __init__(self) -> None:
        self.probed: list[bytes] = []

    def probe(self, path: Path) -> ProbeResult:
        head = path.read_bytes()[:8]
        self.probed.append(head)
        size = path.stat().st_size
        if head.startswith(JPEG_MAGIC):
            return _fixture("jpeg_named_mp4.json", size)
        if head.startswith(b"WEBM"):
            return _fixture("vp9_opus_webm.json", size)
        return _fixture("h264_aac_vertical_mp4.json", size)

    def extract_poster(
        self, source: Path, destination: Path, *, at_ms: int = 0, max_width: int = 1280
    ) -> Path:
        destination.write_bytes(JPEG_MAGIC + b"poster")
        return destination


@pytest.fixture()
def media_settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", media_api_chunk_bytes=CHUNK)


@pytest.fixture()
def media_db(media_settings: Settings) -> Iterator[Database]:
    from runway.db import initialize_database

    database = initialize_database(media_settings)
    yield database
    database.engine.dispose()


@pytest.fixture()
def app_and_client(
    media_settings: Settings, media_db: Database
) -> Iterator[tuple[object, TestClient]]:
    app = create_app(media_settings)
    service: MediaService = app.state.media_service
    service._prober = FakeProber()
    with TestClient(app, base_url=BASE) as client:
        yield app, client


def _service(app: object) -> MediaService:
    return app.state.media_service  # type: ignore[attr-defined, no-any-return]


def _run_probe(app: object, database: Database) -> None:
    service = _service(app)
    queue = JobQueue(database)
    worker = Worker(queue=queue, handlers={PROBE_MEDIA_JOB: service.run_probe_job}, worker_id="t")
    while worker.run_once():
        pass


def _upload(
    client: TestClient, data: bytes, *, filename: str = "clip.mp4", content_type: str = "video/mp4"
) -> int:
    digest = hashlib.sha256(data).hexdigest()
    created = client.post(
        "/api/v1/media",
        json={
            "filename": filename,
            "size_bytes": len(data),
            "content_type": content_type,
            "sha256": digest,
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    plan = body["upload"]
    asset_id = int(body["id"])
    for number in range(1, plan["part_count"] + 1):
        chunk = data[(number - 1) * plan["part_size"] : number * plan["part_size"]]
        sent = client.put(f"/api/v1/media/{asset_id}/parts/{number}", content=chunk)
        assert sent.status_code == 200, sent.text
    done = client.post(f"/api/v1/media/{asset_id}/complete", json={"sha256": digest})
    assert done.status_code == 200, done.text
    return asset_id


def test_a_chunked_upload_resumes_after_a_disconnect_without_resending_parts(
    app_and_client: tuple[object, TestClient], media_db: Database, media_settings: Settings
) -> None:
    app, client = app_and_client
    data = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * (2 * CHUNK // 256 + 200)
    digest = hashlib.sha256(data).hexdigest()
    created = client.post(
        "/api/v1/media",
        json={"filename": "clip.mp4", "size_bytes": len(data), "content_type": "video/mp4",
              "sha256": digest},
    )  # fmt: skip
    assert created.status_code == 201, created.text
    asset = created.json()
    plan = asset["upload"]
    assert asset["status"] == "uploading"
    assert plan["mode"] == "api" and plan["part_size"] == CHUNK and plan["part_count"] == 3
    parts = [data[i : i + CHUNK] for i in range(0, len(data), CHUNK)]

    assert client.put(f"/api/v1/media/{asset['id']}/parts/1", content=parts[0]).status_code == 200
    # The connection drops in the middle of part 2: the server gets half of it.
    dropped = client.put(f"/api/v1/media/{asset['id']}/parts/2", content=parts[1][: CHUNK // 2])
    assert dropped.status_code == 422

    status = client.get(f"/api/v1/media/{asset['id']}/upload").json()
    assert status["received_parts"] == [1]
    assert status["missing_parts"] == [2, 3]
    assert status["bytes_received"] == CHUNK

    sent_bytes = 0
    for number in status["missing_parts"]:
        response = client.put(
            f"/api/v1/media/{asset['id']}/parts/{number}", content=parts[number - 1]
        )
        assert response.status_code == 200
        sent_bytes += len(parts[number - 1])
    assert sent_bytes == len(data) - CHUNK  # part 1 was not sent again

    early = client.get(f"/api/v1/media/{asset['id']}")
    assert early.json()["status"] == "uploading"
    wrong = client.post(f"/api/v1/media/{asset['id']}/complete", json={"sha256": "0" * 64})
    assert wrong.status_code == 422
    done = client.post(f"/api/v1/media/{asset['id']}/complete", json={"sha256": digest})
    assert done.status_code == 200 and done.json()["status"] == "processing"
    with media_db.session() as session:
        assert session.scalar(select(PublishJob.kind)) == PROBE_MEDIA_JOB

    _run_probe(app, media_db)

    ready = client.get(f"/api/v1/media/{asset['id']}").json()
    assert ready["status"] == "ready" and ready["kind"] == "video"
    assert ready["has_poster"] is True
    assert ready["validation"]["youtube_video"] == [] or all(
        issue["severity"] != "error" for issue in ready["validation"]["youtube_video"]
    )
    with media_db.session() as session:
        event = session.scalar(select(EventRecord).where(EventRecord.type == "media.ready"))
        assert event is not None and json.loads(event.payload_json)["status"] == "ready"
        row = session.get(MediaAssetV2, asset["id"])
        assert row is not None
        key = row.storage_key
    # The bytes live outside the /media static mount and are never served from it.
    data_dir = media_settings.resolved_data_dir
    assert (data_dir / "mediastore" / key).is_file()
    assert client.get(f"/media/{key}").status_code == 404
    assert client.get(f"/media/uploads/{key}").status_code == 404
    poster = client.get(f"/api/v1/media/{asset['id']}/poster")
    assert poster.status_code == 200 and poster.headers["content-type"] == "image/jpeg"
    assert poster.headers["x-content-type-options"] == "nosniff"


def test_a_file_with_a_wrong_extension_is_typed_by_its_content(
    app_and_client: tuple[object, TestClient], media_db: Database
) -> None:
    app, client = app_and_client
    asset_id = _upload(client, JPEG_MAGIC + b"\x00" * 5000, filename="holiday.mp4")
    _run_probe(app, media_db)
    asset = client.get(f"/api/v1/media/{asset_id}").json()
    assert asset["kind"] == "image"
    assert asset["content_type"] == "image/jpeg"
    assert "youtube_video" in asset["validation"]
    assert any(issue["severity"] == "error" for issue in asset["validation"]["youtube_video"])


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")
def test_the_real_probe_types_a_jpeg_named_mp4_as_an_image(
    media_settings: Settings, media_db: Database, tmp_path: Path
) -> None:
    image = tmp_path / "frame.jpg"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i",
         "color=c=red:s=320x240", "-frames:v", "1", str(image)],
        check=True, capture_output=True, timeout=60,
    )  # fmt: skip
    app = create_app(media_settings)
    with TestClient(app, base_url=BASE) as client:
        asset_id = _upload(client, image.read_bytes(), filename="clip.mp4")
        _run_probe(app, media_db)
        asset = client.get(f"/api/v1/media/{asset_id}").json()
    assert asset["status"] == "ready", asset
    assert (asset["kind"], asset["content_type"]) == ("image", "image/jpeg")


def test_a_reel_invalid_file_blocks_instagram_and_still_schedules_youtube(
    app_and_client: tuple[object, TestClient], media_db: Database
) -> None:
    app, client = app_and_client
    asset_id = _upload(client, b"WEBM" + b"\x01" * 4000, filename="talk.webm",
                       content_type="video/webm")  # fmt: skip
    _run_probe(app, media_db)
    with media_db.session() as session:
        accounts = []
        for platform in ("youtube", "instagram"):
            account = ConnectedAccount(
                platform=platform,
                external_account_id=f"{platform}-1",
                status="connected",
                granted_scopes_json="[]",
                metadata_json="{}",
            )
            session.add(account)
            session.flush()
            accounts.append(account.id)
    planned = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
    post = client.post(
        "/api/v1/posts",
        json={
            "title": "Talk",
            "body": "New talk",
            "media_ids": [asset_id],
            "targets": [
                {"platform": "youtube", "account_id": accounts[0], "post_type": "youtube_video",
                 "settings": YOUTUBE, "planned_at": planned},
                {"platform": "instagram", "account_id": accounts[1],
                 "post_type": "instagram_reel", "planned_at": planned},
            ],
        },
    ).json()  # fmt: skip
    youtube, instagram = post["targets"]
    assert instagram["state"] == "draft"
    assert any(
        issue["severity"] == "error" and issue["item"] == 0 for issue in instagram["issues"]
    ), instagram["issues"]
    assert youtube["state"] == "ready", youtube["issues"]
    released = client.post(
        f"/api/v1/posts/{post['id']}/targets/{youtube['id']}/release",
        json={"payload_hash": youtube["payload_hash"], "confirmed": True},
    )
    assert released.status_code == 200, released.text
    assert released.json()["targets"][0]["state"] == "queued"


def test_a_target_waiting_on_its_media_becomes_ready_when_the_probe_finishes(
    app_and_client: tuple[object, TestClient], media_db: Database
) -> None:
    app, client = app_and_client
    asset_id = _upload(client, b"\x00\x00\x00\x18ftypmp42" + b"\x02" * 3000)
    with media_db.session() as session:
        account = ConnectedAccount(
            platform="youtube", external_account_id="yt", status="connected",
            granted_scopes_json="[]", metadata_json="{}",
        )  # fmt: skip
        session.add(account)
        session.flush()
        account_id = account.id
    post = client.post(
        "/api/v1/posts",
        json={"title": "Wait", "media_ids": [asset_id],
              "targets": [{"platform": "youtube", "account_id": account_id,
                           "post_type": "youtube_short", "settings": YOUTUBE,
                           "planned_at": (datetime.now(UTC) + timedelta(hours=2)).isoformat()}]},
    ).json()  # fmt: skip
    target = post["targets"][0]
    assert target["state"] == "draft"
    assert "media_not_ready" in {issue["code"] for issue in target["issues"]}
    _run_probe(app, media_db)
    assert client.get(f"/api/v1/posts/{post['id']}").json()["targets"][0]["state"] == "ready"


def test_another_workspace_cannot_reach_an_asset_by_id(
    media_settings: Settings, media_db: Database
) -> None:
    app = create_app(media_settings)
    _service(app)._prober = FakeProber()
    with media_db.session() as session:
        session.add(Workspace(id=2, slug="two", name="Two"))
    with TestClient(app, base_url=BASE) as client:
        asset_id = _upload(client, b"\x00\x00\x00\x18ftypmp42" + b"\x03" * 2000)
        _run_probe(app, media_db)
        app.dependency_overrides[get_principal] = lambda: Principal(
            kind="user", id="other", workspace_id=2, role="owner", scopes=ALL_SCOPES,
            display="Other",
        )  # fmt: skip
        for method, path in (
            ("GET", f"/api/v1/media/{asset_id}"),
            ("GET", f"/api/v1/media/{asset_id}/poster"),
            ("GET", f"/api/v1/media/{asset_id}/upload"),
            ("PUT", f"/api/v1/media/{asset_id}/parts/1"),
            ("DELETE", f"/api/v1/media/{asset_id}"),
        ):
            response = client.request(method, path, content=b"x" if method == "PUT" else None)
            assert response.status_code == 404, (method, path, response.text)
        posted = client.post("/api/v1/posts", json={"title": "Steal", "media_ids": [asset_id]})
        assert posted.status_code == 422
    with media_db.session() as session:
        row = session.get(MediaAssetV2, asset_id)
        assert row is not None and row.status == "ready"
        assert row.storage_key.startswith("w/1/")


def test_media_used_by_a_post_cannot_be_deleted(
    app_and_client: tuple[object, TestClient], media_db: Database
) -> None:
    app, client = app_and_client
    used = _upload(client, b"\x00\x00\x00\x18ftypmp42" + b"\x04" * 1000)
    free = _upload(client, b"\x00\x00\x00\x18ftypmp42" + b"\x05" * 1000)
    _run_probe(app, media_db)
    client.post("/api/v1/posts", json={"title": "Uses it", "media_ids": [used]})
    refused = client.delete(f"/api/v1/media/{used}")
    assert refused.status_code == 409 and refused.json()["code"] == "invalid_state"
    assert client.delete(f"/api/v1/media/{free}").status_code == 204
    assert client.get(f"/api/v1/media/{free}").json()["status"] == "deleted"


class DirectStore:
    """Stands in for R2Store: parts go to signed URLs, never through the API."""

    kind = "r2"

    def __init__(self) -> None:
        self.received: list[int] = []

    def begin_upload(self, key, size, content_type, *, sha256, workspace_id):  # type: ignore[no-untyped-def]
        return UploadPlan(
            store="r2", mode="direct", key=key, upload_id="u-1", size=size, sha256=sha256,
            content_type=content_type, part_size=5 * 1024 * 1024,
            created_at=datetime.now(UTC),
        )  # fmt: skip

    def part_upload_urls(self, plan, part_numbers=None, *, ttl_seconds=3600):  # type: ignore[no-untyped-def]
        numbers = part_numbers or list(plan.part_numbers())
        return {n: f"https://bucket.example.invalid/{plan.key}?partNumber={n}" for n in numbers}

    def receive_part(self, plan, part_number, data):  # type: ignore[no-untyped-def]
        self.received.append(part_number)
        raise AssertionError("bytes must not pass through the API in direct mode")

    def upload_progress(self, plan):  # type: ignore[no-untyped-def]
        return UploadProgress.from_parts(plan, [PartReceipt(1, plan.part_length(1), "e1")])


def test_direct_mode_returns_part_urls_and_refuses_bytes_through_the_api(
    media_settings: Settings, media_db: Database
) -> None:
    app = create_app(media_settings)
    store = DirectStore()
    _service(app).stores["r2"] = store  # type: ignore[assignment]
    with TestClient(app, base_url=BASE) as client:
        created = client.post(
            "/api/v1/media",
            json={"filename": "big.mp4", "size_bytes": 12 * 1024 * 1024,
                  "content_type": "video/mp4", "sha256": "a" * 64},
        )  # fmt: skip
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["storage"] == "r2"
        assert body["upload"]["mode"] == "direct"
        assert sorted(body["upload"]["part_urls"]) == ["1", "2", "3"]
        refused = client.put(f"/api/v1/media/{body['id']}/parts/1", content=b"bytes")
        assert refused.status_code == 409
        status = client.get(f"/api/v1/media/{body['id']}/upload").json()
        assert status["missing_parts"] == [2, 3]
        assert sorted(status["part_urls"]) == ["2", "3"]
    assert store.received == []


def test_also_post_to_creates_a_post_from_an_accepted_proposal(
    app_and_client: tuple[object, TestClient], media_db: Database, media_settings: Settings
) -> None:
    app, client = app_and_client
    image = media_settings.resolved_data_dir / "media" / "approved" / "lineup.jpg"
    image.parent.mkdir(parents=True, exist_ok=True)
    image.write_bytes(JPEG_MAGIC + b"\x06" * 3000)
    with media_db.session() as session:
        session.connection().exec_driver_sql("PRAGMA foreign_keys=OFF")
        asset = MediaAsset(
            kind="candidate", local_path="media/approved/lineup.jpg", mime_type="image/jpeg",
            width=1080, height=1080, file_size=3004, sha256="b" * 64, perceptual_hash="0",
        )  # fmt: skip
        session.add(asset)
        session.flush()
        candidate = CandidateImage(
            search_run_id=1, media_asset_id=asset.id, search_query="q", result_rank=1,
            quality_score=1, style_score=1, novelty_score=1, caption_potential_score=1,
            source_risk_score=0, final_rank_score=1,
        )  # fmt: skip
        session.add(candidate)
        session.flush()
        from runway.db.models import Channel

        channel_id = session.scalar(select(Channel.id).limit(1))
        proposals = []
        for index, status in enumerate(("approved", "needs_review")):
            proposal = Proposal(
                generation_run_id=1, channel_id=channel_id, candidate_image_id=candidate.id,
                planned_publish_at=f"2026-12-0{index + 1}T09:00:00", recommended_caption="Hi",
                alternative_captions_json="[]", final_caption="Look at this", status=status,
                selection_reason="r", style_score=1, novelty_score=1, quality_score=1,
            )  # fmt: skip
            session.add(proposal)
            session.flush()
            proposals.append(proposal.id)
        account = ConnectedAccount(
            platform="instagram", external_account_id="ig", status="connected",
            granted_scopes_json="[]", metadata_json="{}",
        )  # fmt: skip
        session.add(account)
        session.flush()
        account_id = account.id
    body = {"targets": [{"platform": "instagram", "account_id": account_id,
                         "post_type": "instagram_image"}]}  # fmt: skip
    created = client.post(f"/api/v1/proposals/{proposals[0]}/cross-post", json=body)
    assert created.status_code == 201, created.text
    post = created.json()
    assert (post["source"], post["source_ref"]) == ("proposal", str(proposals[0]))
    assert post["body"] == "Look at this"
    assert post["targets"][0]["state"] == "draft"  # waits for its media check
    assert post["targets"][0]["release"]["state"] == "none"
    _run_probe(app, media_db)
    after = client.get(f"/api/v1/posts/{post['id']}").json()
    assert after["media"][0]["status"] == "ready"
    refused = client.post(f"/api/v1/proposals/{proposals[1]}/cross-post", json=body)
    assert refused.status_code == 409
    with media_db.session() as session:
        proposal = session.get(Proposal, proposals[0])
        assert proposal is not None and proposal.status == "approved"  # untouched
