from __future__ import annotations

import pytest

from runway.db.base import Database
from runway.db.models import ConnectedAccount, PlatformContent
from runway.platforms.drafts import DraftService, external_id_candidates


@pytest.mark.parametrize(
    ("platform", "url", "expected"),
    [
        ("youtube", "https://www.youtube.com/watch?v=abc123XYZ_-", "abc123XYZ_-"),
        ("youtube", "https://www.youtube.com/shorts/shortId_99", "shortId_99"),
        ("youtube", "https://youtu.be/ytbeId12345", "ytbeId12345"),
        ("tiktok", "https://www.tiktok.com/@user/video/7312345678901234", "7312345678901234"),
        ("instagram", "https://www.instagram.com/p/Cxyz123/", None),
    ],
)
def test_external_id_candidates(platform: str, url: str, expected: str | None) -> None:
    candidates = external_id_candidates(platform, url)
    assert candidates[0] == url
    if expected is None:
        assert candidates == [url]
    else:
        assert expected in candidates


def _account(database: Database, platform: str, external_id: str) -> ConnectedAccount:
    with database.session() as session:
        account = ConnectedAccount(
            platform=platform,
            external_account_id=external_id,
            status="connected",
        )
        session.add(account)
        session.flush()
        session.expunge(account)
        return account


def test_mark_published_matches_by_derived_external_id(database: Database) -> None:
    account = _account(database, "youtube", "UCx")
    service = DraftService(database, database.settings)
    with database.session() as session:
        session.add(
            PlatformContent(
                account_id=account.id,
                platform="youtube",
                external_id="abc123XYZ_-",
                content_type="video",
            )
        )
    draft = service.create_draft(
        title="d1", targets=[{"platform": "youtube", "account_id": account.id}]
    )
    target_id = draft["targets"][0]["id"]
    view = service.mark_published(draft["id"], target_id, "https://youtu.be/abc123XYZ_-")
    target = view["targets"][0]
    assert target["status"] == "published"
    assert target["published_content_id"] is not None
    assert view["status"] == "published"


def test_mark_published_matches_by_permalink_for_instagram(
    database: Database,
) -> None:
    account = _account(database, "instagram", "ig1")
    service = DraftService(database, database.settings)
    with database.session() as session:
        session.add(
            PlatformContent(
                account_id=account.id,
                platform="instagram",
                external_id="ig-media-1",
                content_type="image",
                permalink="https://www.instagram.com/p/Cxyz123/",
            )
        )
    draft = service.create_draft(
        title="d2", targets=[{"platform": "instagram", "account_id": account.id}]
    )
    target_id = draft["targets"][0]["id"]
    view = service.mark_published(draft["id"], target_id, "https://www.instagram.com/p/Cxyz123/")
    assert view["targets"][0]["published_content_id"] is not None


def test_target_without_account_binds_to_single_connected_account(
    database: Database,
) -> None:
    account = _account(database, "youtube", "UCsolo")
    service = DraftService(database, database.settings)
    draft = service.create_draft(title="d3", targets=[{"platform": "youtube"}])
    assert draft["targets"][0]["account_id"] == account.id


def test_target_without_account_stays_unbound_when_multiple(
    database: Database,
) -> None:
    _account(database, "youtube", "UCone")
    _account(database, "youtube", "UCtwo")
    service = DraftService(database, database.settings)
    draft = service.create_draft(title="d4", targets=[{"platform": "youtube"}])
    assert draft["targets"][0]["account_id"] is None
