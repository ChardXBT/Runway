from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from runway.config import Settings
from runway.platforms.base import (
    AccountIdentity,
    CapabilityUnsupported,
    CommentItem,
    ContentItem,
    DailyMetric,
    Page,
    PlatformProvider,
    ReplyResult,
    TokenSet,
)
from runway.platforms.capabilities import CapabilityEntry, Platform, capability_matrix

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures"

MOCK_TOKEN = "mock-access-token"


def load_fixture(platform: Platform) -> dict[str, Any]:
    path = FIXTURE_DIR / f"{platform.value}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError(f"mock fixture {path} must contain a JSON object")
    return payload


def mock_token(platform: Platform) -> TokenSet:
    return TokenSet(
        access_token=MOCK_TOKEN,
        refresh_token=None,
        expires_at=None,
        scopes=[],
        extra={"platform": platform.value, "mock": "true"},
    )


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class MockPlatformProvider(PlatformProvider):
    """Deterministic fictional provider used when RUNWAY_PLATFORM_PROVIDER=mock."""

    def __init__(
        self,
        platform: Platform,
        settings: Settings,
        fixture: dict[str, Any] | None = None,
    ) -> None:
        self.platform = platform
        self.settings = settings
        self.fixture = fixture if fixture is not None else load_fixture(platform)
        self.warnings: list[str] = []

    def capabilities(self) -> list[CapabilityEntry]:
        return capability_matrix(self.platform)

    def configured(self) -> bool:
        return True

    def authorization_url(
        self,
        *,
        state: str,
        redirect_uri: str,
        code_challenge: str | None,
    ) -> str:
        raise CapabilityUnsupported("mock mode has no OAuth")

    def exchange_code(
        self,
        *,
        code: str,
        redirect_uri: str,
        code_verifier: str | None,
    ) -> TokenSet:
        raise CapabilityUnsupported("mock mode has no OAuth")

    def refresh(self, token: TokenSet) -> TokenSet:
        return token

    def revoke(self, token: TokenSet) -> bool:
        return True

    def fetch_identity(self, token: TokenSet) -> AccountIdentity:
        identity = dict(self.fixture.get("identity") or {})
        identity.pop("uploads_playlist", None)
        return AccountIdentity.model_validate(identity)

    def list_content(
        self,
        token: TokenSet,
        *,
        cursor: dict[str, Any] | None,
        limit: int,
    ) -> Page[ContentItem]:
        items = self.fixture.get("content") or []
        offset = int(cursor.get("offset", 0)) if cursor else 0
        page_items = items[offset : offset + max(limit, 1)]
        parsed = [ContentItem.model_validate(item) for item in page_items]
        next_offset = offset + len(page_items)
        return Page(
            items=parsed,
            next_cursor={"offset": next_offset} if next_offset < len(items) else None,
        )

    def fetch_content_metrics(
        self,
        token: TokenSet,
        external_ids: list[str],
    ) -> dict[str, dict[str, int | float | None]]:
        metrics: dict[str, dict[str, int | float | None]] = {}
        wanted = set(external_ids)
        for item in self.fixture.get("content") or []:
            external_id = str(item.get("external_id") or "")
            if external_id in wanted:
                values = item.get("metrics") or {}
                metrics[external_id] = {str(key): value for key, value in values.items()}
        return metrics

    def fetch_account_daily_metrics(
        self,
        token: TokenSet,
        *,
        since: date,
        until: date,
    ) -> list[DailyMetric]:
        source = "mock_fixtures"
        results: list[DailyMetric] = []
        for row in self.fixture.get("daily_metrics") or []:
            day = date.fromisoformat(str(row["day"]))
            if day < since or day > until:
                continue
            for key, value in row.items():
                if key == "day" or not isinstance(value, (int, float)):
                    continue
                results.append(DailyMetric(day=day, metric=key, value=float(value), source=source))
        return results

    def list_comments(
        self,
        token: TokenSet,
        *,
        content_external_id: str,
        cursor: dict[str, Any] | None,
        limit: int,
    ) -> Page[CommentItem]:
        if self.platform is Platform.tiktok:
            raise CapabilityUnsupported("TikTok comments require the Business Account API")
        flat: list[CommentItem] = []
        owner_id = str((self.fixture.get("identity") or {}).get("external_account_id") or "")
        for top in self.fixture.get("comments") or []:
            flat.append(
                CommentItem(
                    external_id=str(top["external_id"]),
                    content_external_id=content_external_id,
                    author_name=top.get("author_name"),
                    author_external_id=top.get("author_external_id"),
                    author_is_owner=bool(top.get("author_is_owner")),
                    text=str(top.get("text") or ""),
                    like_count=top.get("like_count"),
                    reply_count=len(top.get("replies") or []),
                    published_at=_parse_dt(top.get("published_at")),
                    raw=top,
                )
            )
            for reply in top.get("replies") or []:
                flat.append(
                    CommentItem(
                        external_id=str(reply["external_id"]),
                        content_external_id=content_external_id,
                        parent_external_id=str(top["external_id"]),
                        author_name=reply.get("author_name"),
                        author_external_id=reply.get("author_external_id"),
                        author_is_owner=bool(
                            reply.get("author_is_owner")
                            or reply.get("author_external_id") == owner_id
                        ),
                        text=str(reply.get("text") or ""),
                        like_count=reply.get("like_count"),
                        published_at=_parse_dt(reply.get("published_at")),
                        raw=reply,
                    )
                )
        flat.sort(key=lambda item: item.published_at or datetime.min.replace(tzinfo=UTC))
        offset = int(cursor.get("offset", 0)) if cursor else 0
        page_items = flat[offset : offset + max(limit, 1)]
        next_offset = offset + len(page_items)
        return Page(
            items=page_items,
            next_cursor={"offset": next_offset} if next_offset < len(flat) else None,
        )

    def reply_to_comment(
        self,
        token: TokenSet,
        *,
        comment_external_id: str,
        text: str,
    ) -> ReplyResult:
        if self.platform is Platform.tiktok:
            raise CapabilityUnsupported("TikTok comment replies require the Business Account API")
        if not self.settings.inbox_replies_enabled:
            raise CapabilityUnsupported("comment replies require RUNWAY_INBOX_REPLIES_ENABLED=true")
        return ReplyResult(
            external_id=f"mock_reply_{comment_external_id}",
            verified=True,
            detail="mock reply recorded without a network call",
        )
