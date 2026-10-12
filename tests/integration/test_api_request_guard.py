from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from runway.api.app import create_app
from runway.config import Settings

WEB_ORIGIN = "http://127.0.0.1:3000"


@pytest.fixture()
def client(settings: Settings) -> Iterator[TestClient]:
    with TestClient(create_app(settings), base_url="http://127.0.0.1:8000") as test_client:
        yield test_client


@pytest.mark.parametrize("host", ["127.0.0.1:8000", "localhost:8000", "[::1]:8000", "LOCALHOST"])
def test_loopback_hosts_are_served(client: TestClient, host: str) -> None:
    response = client.get("/health", headers={"host": host})

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


@pytest.mark.parametrize(
    "host", ["attacker.example:8000", "127.0.0.1.attacker.example", "", "0.0.0.0:8000"]
)
def test_rebinding_hosts_are_refused_for_reads_and_media(client: TestClient, host: str) -> None:
    for path in ("/health", "/api/settings", "/media/anything.png"):
        response = client.get(path, headers={"host": host})
        assert response.status_code == 421


def test_web_origin_may_mutate_and_receives_cors_headers(client: TestClient) -> None:
    response = client.post(
        "/api/candidates/999999/reject",
        json={"reason": "user_rejected"},
        headers={"origin": WEB_ORIGIN, "sec-fetch-site": "same-site"},
    )

    assert response.status_code == 404
    assert response.headers["access-control-allow-origin"] == WEB_ORIGIN


@pytest.mark.parametrize("origin", ["https://attacker.example", "null", "http://127.0.0.1:9999"])
def test_foreign_origins_cannot_mutate(client: TestClient, origin: str) -> None:
    for method, path in (
        ("POST", "/api/publisher/queue/resume"),
        ("POST", "/api/analysis/history"),
        ("PATCH", "/api/settings/full"),
    ):
        response = client.request(method, path, headers={"origin": origin})
        assert response.status_code == 403, (method, path)


def test_cross_site_requests_without_origin_cannot_mutate(client: TestClient) -> None:
    response = client.post("/api/profiles/build", headers={"sec-fetch-site": "cross-site"})

    assert response.status_code == 403


def test_non_browser_clients_and_safe_reads_are_unaffected(client: TestClient) -> None:
    assert client.get("/api/settings", headers={"origin": "https://attacker.example"}).is_success
    response = client.post("/api/candidates/999999/reject", json={"reason": "user_rejected"})
    assert response.status_code == 404
