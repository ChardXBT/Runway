from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select

from runway.config import Settings
from runway.db import initialize_database
from runway.db.base import Database
from runway.db.models import (
    ConnectedAccount,
    ContentDraft,
    DraftTarget,
    PlatformContent,
    SyncCursor,
)
from runway.platforms.base import AccountIdentity, ContentItem, Page, TokenSet, iso_utc
from runway.platforms.capabilities import Platform, capability_matrix, capability_ready
from runway.platforms.content import ContentService
from runway.platforms.mock import MockPlatformProvider, mock_token
from runway.platforms.registry import ProviderRegistry
from runway.platforms.sync import SyncService


def test_iso_utc_attaches_offset_to_naive_datetimes() -> None:
    naive = datetime(2026, 7, 20, 12, 0, 0)
    assert iso_utc(naive) == "2026-07-20T12:00:00+00:00"
    aware = datetime(2026, 7, 20, 12, 0, 0, tzinfo=UTC)
    assert iso_utc(aware) == "2026-07-20T12:00:00+00:00"
    assert iso_utc(None) is None


@pytest.fixture()
def platform_env(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        platform_sync_max_content_items=20,
    )
    database = initialize_database(settings)
    with database.session() as session:
        account = ConnectedAccount(
            platform="youtube",
            external_account_id="UCtest",
            handle="@test",
            display_name="Test",
            status="connected",
            granted_scopes_json="[]",
            token_ref="t1",
        )
        session.add(account)
    registry = ProviderRegistry(database, settings)
    service = SyncService(database, settings, registry)
    yield settings, database, registry, service
    database.engine.dispose()


def _account(database: Database) -> ConnectedAccount:
    with database.session() as session:
        account = session.scalar(select(ConnectedAccount))
        assert account is not None
        session.expunge(account)
        return account


def _make_fixture(count: int) -> dict[str, Any]:
    return {
        "identity": {
            "external_account_id": "UCtest",
            "handle": "@test",
            "display_name": "Test",
        },
        "content": [
            {
                "external_id": f"vid{i:03d}",
                "content_type": "video",
                "title": f"Video {i}",
                "permalink": f"https://www.youtube.com/watch?v=vid{i:03d}",
                "published_at": "2026-07-01T10:00:00Z",
                "metrics": {"views": i},
            }
            for i in range(count)
        ],
    }


class PagedMockProvider(MockPlatformProvider):
    """Mock provider that pages at most 10 items per call."""

    page_size = 10

    def __init__(self, fixture: dict[str, Any], settings: Settings) -> None:
        super().__init__(Platform.youtube, settings, fixture=fixture)
        self.calls = 0

    def list_content(
        self,
        token: TokenSet,
        *,
        cursor: dict[str, Any] | None,
        limit: int,
    ) -> Page[ContentItem]:
        self.calls += 1
        return super().list_content(token, cursor=cursor, limit=min(limit, self.page_size))


def _cursor(database: Database, account_id: int) -> dict[str, Any]:
    with database.session() as session:
        row = session.scalar(
            select(SyncCursor).where(
                SyncCursor.account_id == account_id,
                SyncCursor.scope == "content",
            )
        )
        return json.loads(row.cursor_json) if row else {}


def _content_count(database: Database, account_id: int) -> int:
    with database.session() as session:
        rows = session.scalars(
            select(PlatformContent).where(PlatformContent.account_id == account_id)
        ).all()
        return len(rows)


def test_content_cursor_head_refresh_and_backfill(platform_env) -> None:
    settings, database, _registry, service = platform_env
    account = _account(database)
    provider = PagedMockProvider(_make_fixture(45), settings)
    token = mock_token(Platform.youtube)

    service._step_content(provider, account.id, token)
    assert _content_count(database, account.id) == 20
    assert _cursor(database, account.id).get("resume") == {"offset": 20}

    # A new upload lands at the head before run 2.
    provider.fixture["content"].insert(
        0,
        {
            "external_id": "newhead",
            "content_type": "video",
            "title": "Fresh upload",
            "permalink": "https://www.youtube.com/watch?v=newhead",
            "published_at": "2026-07-20T10:00:00Z",
            "metrics": {"views": 1},
        },
    )
    service._step_content(provider, account.id, token)
    with database.session() as session:
        fresh = session.scalar(
            select(PlatformContent).where(PlatformContent.external_id == "newhead")
        )
    assert fresh is not None, "head refresh must import new uploads"
    assert _cursor(database, account.id).get("resume") == {"offset": 30}

    # Subsequent runs keep backfilling until the inventory is exhausted.
    service._step_content(provider, account.id, token)
    service._step_content(provider, account.id, token)
    assert _cursor(database, account.id).get("complete") is True
    assert _content_count(database, account.id) == 46

    # Once complete, a run only performs the head fetch.
    provider.calls = 0
    service._step_content(provider, account.id, token)
    assert provider.calls == 1


def test_sync_links_published_draft_targets(platform_env) -> None:
    settings, database, _registry, service = platform_env
    account = _account(database)
    with database.session() as session:
        draft = ContentDraft(title="Cross-post", body="body", status="draft")
        session.add(draft)
        session.flush()
        session.add(
            DraftTarget(
                draft_id=draft.id,
                platform="youtube",
                account_id=account.id,
                status="published",
                published_url="https://www.youtube.com/watch?v=vid007",
            )
        )
    provider = PagedMockProvider(_make_fixture(15), settings)
    service._step_content(provider, account.id, mock_token(Platform.youtube))
    with database.session() as session:
        target = session.scalar(select(DraftTarget))
        linked = session.scalar(
            select(PlatformContent).where(PlatformContent.external_id == "vid007")
        )
        assert target is not None and linked is not None
        assert target.published_content_id == linked.id


def test_missing_scopes_and_blocked_by_setting(tmp_path: Path) -> None:
    settings = Settings(
        data_dir=tmp_path / "data",
        platform_provider="mock",
        inbox_replies_enabled=False,
        youtube_engage_scopes=False,
    )
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    registry.ensure_mock_accounts()
    with database.session() as session:
        account = session.scalar(
            select(ConnectedAccount).where(ConnectedAccount.platform == "youtube")
        )
        assert account is not None
        granted = json.loads(account.granted_scopes_json)
        assert "youtube.readonly" in " ".join(granted) or granted, (
            "mock accounts must be seeded with the platform scope list"
        )
        session.expunge(account)

    from runway.api.platform_routes import _account_view

    view = _account_view(database, settings, account)
    capabilities = {cap["capability"]: cap for cap in view["capabilities"]}
    analytics = capabilities["account_analytics"]
    assert analytics["ready"] is True
    assert analytics["missing_scopes"] == []
    replies = capabilities["comments_reply"]
    assert replies["blocked_by_setting"] is True
    assert replies["ready"] is False
    assert isinstance(replies["missing_scopes"], list)
    database.engine.dispose()


def test_required_env_values() -> None:
    from runway.api.platform_routes import REQUIRED_ENV

    assert REQUIRED_ENV[Platform.youtube] == [
        "RUNWAY_YOUTUBE_CLIENT_ID",
        "RUNWAY_YOUTUBE_CLIENT_SECRET",
    ]
    assert REQUIRED_ENV[Platform.instagram] == [
        "RUNWAY_INSTAGRAM_APP_ID",
        "RUNWAY_INSTAGRAM_APP_SECRET",
    ]
    assert REQUIRED_ENV[Platform.tiktok] == [
        "RUNWAY_TIKTOK_CLIENT_KEY",
        "RUNWAY_TIKTOK_CLIENT_SECRET",
    ]


def test_content_counts_respect_filters(tmp_path: Path) -> None:
    settings = Settings(data_dir=tmp_path / "data")
    database = initialize_database(settings)
    with database.session() as session:
        youtube = ConnectedAccount(
            platform="youtube",
            external_account_id="a",
            status="connected",
        )
        instagram = ConnectedAccount(
            platform="instagram",
            external_account_id="b",
            status="connected",
        )
        session.add_all([youtube, instagram])
        session.flush()
        for index, (account, ctype) in enumerate(
            [(youtube, "video"), (youtube, "short"), (instagram, "image")]
        ):
            session.add(
                PlatformContent(
                    account_id=account.id,
                    platform=account.platform,
                    external_id=f"x{index}",
                    content_type=ctype,
                    title=f"t{index}",
                )
            )
    service = ContentService(database)
    result = service.list_items(platform="youtube")
    assert result["total"] == 2
    assert result["counts"]["by_platform"] == {"youtube": 2, "instagram": 1}
    assert result["counts"]["by_type"] == {"video": 1, "short": 1}
    typed = service.list_items(platform="youtube", content_type="short")
    # by_type ignores the type filter (shows all types under the other filters);
    # by_platform ignores the platform filter but keeps the type filter.
    assert typed["counts"]["by_type"] == {"short": 1, "video": 1}
    assert typed["counts"]["by_platform"] == {"youtube": 1}
    database.engine.dispose()


def test_capability_matrix_union_matches_mock_grants() -> None:
    entries = capability_matrix(Platform.instagram, engage_scopes=False, replies_enabled=False)
    required = sorted({s for e in entries for s in e.requires_scopes})
    assert required
    ready = capability_ready(entries[1], account_status="connected", granted_scopes=required)
    assert ready is True


def test_account_identity_normalizes_handles() -> None:
    assert AccountIdentity(external_account_id="x", handle="@name").handle == "name"
    assert AccountIdentity(external_account_id="x", handle="@@name").handle == "name"
    assert AccountIdentity(external_account_id="x", handle="name").handle == "name"
    assert AccountIdentity(external_account_id="x", handle="@").handle is None
    assert AccountIdentity(external_account_id="x", handle=None).handle is None


def test_mock_seed_backdates_metric_snapshots(tmp_path: Path) -> None:
    from runway.db.models import AccountMetricSnapshot, ContentMetricSnapshot

    settings = Settings(data_dir=tmp_path / "data", platform_provider="mock")
    database = initialize_database(settings)
    registry = ProviderRegistry(database, settings)
    registry.ensure_mock_accounts()
    with database.session() as session:
        accounts = session.scalars(select(ConnectedAccount)).all()
        assert accounts
        for account in accounts:
            snapshots = session.scalars(
                select(AccountMetricSnapshot).where(AccountMetricSnapshot.account_id == account.id)
            ).all()
            assert len(snapshots) >= 8
            contents = session.scalars(
                select(PlatformContent).where(PlatformContent.account_id == account.id)
            ).all()
            assert contents
            for content in contents:
                history = session.scalars(
                    select(ContentMetricSnapshot).where(
                        ContentMetricSnapshot.content_id == content.id
                    )
                ).all()
                assert len(history) >= 3
        session.expunge_all()
        assert all(a.handle is None or not a.handle.startswith("@") for a in accounts)
    database.engine.dispose()
