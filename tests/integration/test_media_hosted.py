"""Hosted `/api/v1/media`: direct browser-to-R2 uploads (spec 02 section 4, spec 05).

In the hosted profile ``POST /api/v1/media`` returns signed part URLs and the
API never carries media bytes; the poster frame lives as an R2 object and the
poster route redirects to a short-lived signed URL. A real ``R2Store`` runs
against the in-memory fake in ``tests/r2_support.py``; no network.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from r2_support import PART, Clock, FakeR2, _payload, fake_r2_browser, fake_r2_store
from sqlalchemy import select

from runway.api.app import create_app
from runway.auth.principal import ALL_SCOPES, Principal, get_principal
from runway.config import Settings
from runway.db.base import Database
from runway.db.models import MediaAssetV2, MediaUpload, PublishJob
from runway.jobs.queue import JobQueue, JobState
from runway.jobs.worker import Worker
from runway.mediastore import ProbeResult, parse_ffprobe
from runway.posts.jobs import PROBE_MEDIA_JOB
from runway.posts.media_service import MediaService

BASE = "http://127.0.0.1:8000"
FIXTURES = Path(__file__).resolve().parents[1] / "unit" / "fixtures" / "ffprobe"
JPEG_MAGIC = b"\xff\xd8\xff\xe0"
# hosted resolves the database token store: the API validates the keyring at startup.
_TOKEN_KEYRING = "v1:" + "a" * 43 + "="


def _fixture(name: str, size: int) -> ProbeResult:
    payload = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return parse_ffprobe(payload, size_bytes=size)


class FakeProber:
    """Types the file by its bytes: JPEG magic is an image, else H.264 video."""

    def probe(self, path: Path) -> ProbeResult:
        head = path.read_bytes()[:8]
        size = path.stat().st_size
        if head.startswith(JPEG_MAGIC):
            return _fixture("jpeg_named_mp4.json", size)
        return _fixture("h264_aac_vertical_mp4.json", size)

    def extract_poster(
        self, source: Path, destination: Path, *, at_ms: int = 0, max_width: int = 1280
    ) -> Path:
        destination.write_bytes(JPEG_MAGIC + b"poster")
        return destination


@pytest.fixture()
def hosted_settings(tmp_path: Path) -> Settings:
    settings = Settings(data_dir=tmp_path / "data").model_copy(update={"profile": "hosted"})
    settings.token_keys = SecretStr(_TOKEN_KEYRING)
    return settings


@pytest.fixture()
def hosted_db(hosted_settings: Settings) -> Iterator[Database]:
    from runway.db import initialize_database

    database = initialize_database(hosted_settings)
    yield database
    database.engine.dispose()


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def fake(clock: Clock) -> FakeR2:
    return FakeR2(clock)


@pytest.fixture()
def hosted_app(
    hosted_settings: Settings, hosted_db: Database, fake: FakeR2, clock: Clock
) -> Iterator[tuple[object, TestClient, MediaService]]:
    app = create_app(hosted_settings)
    service: MediaService = app.state.media_service
    service._prober = FakeProber()
    service.stores["r2"] = fake_r2_store(fake, clock)
    app.dependency_overrides[get_principal] = lambda: Principal(
        kind="user", id="1", workspace_id=1, role="owner", scopes=ALL_SCOPES, display="Owner"
    )
    with TestClient(app, base_url=BASE) as client:
        yield app, client, service


def _run_probe(database: Database, service: MediaService) -> None:
    queue = JobQueue(database)
    worker = Worker(queue=queue, handlers={PROBE_MEDIA_JOB: service.run_probe_job}, worker_id="t")
    while worker.run_once():
        pass


def _begin(client: TestClient, data: bytes) -> tuple[int, dict[str, object]]:
    created = client.post(
        "/api/v1/media",
        json={
            "filename": "clip.mp4",
            "size_bytes": len(data),
            "content_type": "video/mp4",
            "sha256": hashlib.sha256(data).hexdigest(),
        },
    )
    assert created.status_code == 201, created.text
    body = created.json()
    return int(body["id"]), body


def test_hosted_upload_is_direct_and_the_api_never_carries_media_bytes(
    hosted_app: tuple[object, TestClient, MediaService], fake: FakeR2, hosted_db: Database
) -> None:
    _app, client, service = hosted_app
    data = _payload(2 * PART + 321)
    digest = hashlib.sha256(data).hexdigest()
    asset_id, body = _begin(client, data)
    assert body["storage"] == "r2"
    plan = body["upload"]
    assert plan["mode"] == "direct" and plan["part_size"] == PART and plan["part_count"] == 3

    # Every part URL is presigned and carries the part's exact content length.
    urls = {int(number): url for number, url in plan["part_urls"].items()}
    assert sorted(urls) == [1, 2, 3]
    for url in urls.values():
        query = parse_qs(urlparse(url).query)
        assert "content-length" in query["X-Amz-SignedHeaders"][0].split(";")
        assert int(query["X-Amz-Expires"][0]) > 0

    # Bytes can never reach the API in direct mode.
    refused = client.put(f"/api/v1/media/{asset_id}/parts/1", content=data[:PART])
    assert refused.status_code == 409

    # The browser uploads its parts straight to storage.
    browser = fake_r2_browser(fake)
    for number, url in urls.items():
        chunk = data[(number - 1) * PART : number * PART]
        assert browser.put(url, content=chunk).status_code == 200

    status = client.get(f"/api/v1/media/{asset_id}/upload").json()
    assert status["mode"] == "direct" and status["missing_parts"] == []

    done = client.post(f"/api/v1/media/{asset_id}/complete", json={"sha256": digest})
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "processing"

    _run_probe(hosted_db, service)

    ready = client.get(f"/api/v1/media/{asset_id}").json()
    assert ready["status"] == "ready" and ready["kind"] == "video"
    assert ready["has_poster"] is True

    # The probe stored the poster frame as an object; no local file exists.
    with hosted_db.session() as session:
        row = session.get(MediaAssetV2, asset_id)
        assert row is not None and row.poster_key is not None
        assert row.poster_key.startswith("w/1/") and row.poster_key.endswith(".jpg")
        assert row.storage_key in fake.objects
        assert fake.objects[row.storage_key] == data
        assert fake.objects[row.poster_key] == JPEG_MAGIC + b"poster"
    poster_dir = hosted_db.settings.resolved_data_dir / "mediastore-posters"
    assert not poster_dir.exists() or not list(poster_dir.iterdir())

    # The preview is a short-lived signed URL on the storage domain, never the API origin.
    preview = client.get(f"/api/v1/media/{asset_id}/poster", follow_redirects=False)
    assert preview.status_code == 307
    location = preview.headers["location"]
    assert location.startswith("https://") and "r2.cloudflarestorage.com" in location
    preview_query = parse_qs(urlparse(location).query)
    assert int(preview_query["X-Amz-Expires"][0]) <= 900
    fetched = browser.get(location)
    assert fetched.status_code == 200 and fetched.content == JPEG_MAGIC + b"poster"
    assert fetched.headers["content-disposition"] == "attachment"

    # Every request the API process itself signed carried a small body: the
    # media bytes only ever went browser -> R2 (presigned) or worker -> R2 (GET).
    assert max(len(request.content) for request in fake.signed_requests) < 10_000


def test_hosted_complete_still_verifies_the_total_size(
    hosted_app: tuple[object, TestClient, MediaService], fake: FakeR2, hosted_db: Database
) -> None:
    _app, client, _service = hosted_app
    data = _payload(PART + 10)
    asset_id, _body = _begin(client, data)
    with hosted_db.session() as session:
        upload = session.scalar(select(MediaUpload).where(MediaUpload.asset_id == asset_id))
        assert upload is not None
        upload_id = upload.upload_id
    # A part reached the bucket truncated (for example a store that ignored the
    # signed length): completion must refuse, and the asset is rejected.
    fake.uploads[upload_id]["parts"][1] = (b"short", hashlib.md5(b"short").hexdigest())
    fake.uploads[upload_id]["parts"][2] = (data[PART:], hashlib.md5(data[PART:]).hexdigest())
    done = client.post(
        f"/api/v1/media/{asset_id}/complete",
        json={"sha256": hashlib.sha256(data).hexdigest()},
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "rejected"


def test_hosted_media_upload_fails_closed_without_the_r2_store(
    hosted_settings: Settings, hosted_db: Database
) -> None:
    app = create_app(hosted_settings)
    app.dependency_overrides[get_principal] = lambda: Principal(
        kind="user", id="1", workspace_id=1, role="owner", scopes=ALL_SCOPES, display="Owner"
    )
    with TestClient(app, base_url=BASE) as client:
        created = client.post(
            "/api/v1/media",
            json={
                "filename": "clip.mp4",
                "size_bytes": 100,
                "content_type": "video/mp4",
                "sha256": "a" * 64,
            },
        )
        # capability_not_ready: the API must not fall back to streaming bytes.
        assert created.status_code == 409, created.text
        assert created.json()["code"] == "capability_not_ready"


def test_local_profile_keeps_the_api_upload_mode(
    tmp_path: Path, fake: FakeR2, clock: Clock
) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    from runway.db import initialize_database

    database = initialize_database(settings)
    app = create_app(settings)
    # Even with an R2 store present, local uploads keep going through the API.
    service: MediaService = app.state.media_service
    service.stores["r2"] = fake_r2_store(fake, clock)
    with TestClient(app, base_url=BASE) as client:
        created = client.post(
            "/api/v1/media",
            json={
                "filename": "clip.mp4",
                "size_bytes": 100,
                "content_type": "video/mp4",
                "sha256": "a" * 64,
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["storage"] == "local"
        assert body["upload"]["mode"] == "api"
        assert body["upload"]["part_path"] == f"/api/v1/media/{body['id']}/parts/{{n}}"
        assert body["upload"]["part_urls"] is None
    database.engine.dispose()


# -------------------------------------------------------- workspace exports
#
# Hosted API and worker share no disk: the export job puts the artifact in
# object storage, so the download keeps working after `purge_finished_jobs`
# retires the tracking row (the job row is only a progress hint).


def test_export_download_survives_the_tracking_jobs_retention_purge(
    hosted_app: tuple[object, TestClient, MediaService],
    hosted_settings: Settings,
    hosted_db: Database,
    fake: FakeR2,
    clock: Clock,
    tmp_path: Path,
) -> None:
    from types import SimpleNamespace

    from runway.jobs.retention import purge_finished_jobs
    from runway.workspaces.lifecycle import export_key, run_export_workspace_job

    _app, client, _service = hosted_app
    requested = client.post("/api/v1/workspace/export")
    assert requested.status_code == 202, requested.text
    export_id = requested.json()["export_id"]
    # The id doubles as the artifact key's digest component (16 lowercase hex).
    assert re.fullmatch(r"[0-9a-f]{16}", export_id)

    # The worker ran on another machine: the file landed on ITS data dir and
    # the artifact went to the object store the API shares.
    worker_settings = hosted_settings.model_copy(update={"data_dir": tmp_path / "worker-data"})
    job = SimpleNamespace(payload={"workspace_id": 1, "export_id": export_id})
    run_export_workspace_job(
        hosted_db, worker_settings, job, stores={"r2": fake_r2_store(fake, clock)}
    )
    key = export_key(1, export_id)
    assert key in fake.objects

    # The export job finished and aged past the 14-day finished-job retention.
    with hosted_db.session() as session:
        row = session.scalar(
            select(PublishJob).where(PublishJob.idempotency_key == f"export:{export_id}")
        )
        assert row is not None
        row.state = JobState.COMPLETED.value
        row.updated_at = clock.now - timedelta(days=30)
        session.flush()
        purge_finished_jobs(session, now=clock.now)
        session.flush()
        assert (
            session.scalar(
                select(PublishJob).where(PublishJob.idempotency_key == f"export:{export_id}")
            )
            is None
        )

    download = client.get(f"/api/v1/workspace/exports/{export_id}", follow_redirects=False)
    assert download.status_code == 307, download.text
    location = download.headers["location"]
    assert location.startswith("https://") and "r2.cloudflarestorage.com" in location
    fetched = fake_r2_browser(fake).get(location)
    assert fetched.status_code == 200
    document = json.loads(fetched.content)
    assert document["format"] == "runway-workspace-export"
    assert fetched.headers["content-disposition"].startswith("attachment")


def test_export_download_is_409_while_the_job_is_pending(
    hosted_app: tuple[object, TestClient, MediaService],
) -> None:
    _app, client, _service = hosted_app
    requested = client.post("/api/v1/workspace/export")
    assert requested.status_code == 202, requested.text
    export_id = requested.json()["export_id"]
    # No artifact anywhere and the job is still queued: keep polling.
    download = client.get(f"/api/v1/workspace/exports/{export_id}", follow_redirects=False)
    assert download.status_code == 409
