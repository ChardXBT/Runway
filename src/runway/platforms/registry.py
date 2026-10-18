from __future__ import annotations

import json
import logging

from sqlalchemy import select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ConnectedAccount
from runway.platforms.base import PlatformProvider
from runway.platforms.capabilities import (
    DEFAULT_PLATFORM_SCOPES,
    Platform,
)
from runway.platforms.instagram import InstagramProvider
from runway.platforms.mock import MockPlatformProvider, mock_token
from runway.platforms.tiktok import TikTokProvider
from runway.platforms.tokens import TokenStore
from runway.platforms.youtube import YouTubeProvider

logger = logging.getLogger(__name__)


class UnknownPlatformError(ValueError):
    pass


def parse_platform(value: str) -> Platform:
    try:
        return Platform(value)
    except ValueError as exc:
        raise UnknownPlatformError(f"unknown platform {value!r}") from exc


class ProviderRegistry:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings
        self.token_store = TokenStore(settings.resolved_data_dir / "secrets")
        self._providers: dict[Platform, PlatformProvider] = {}

    @property
    def provider_mode(self) -> str:
        return self.settings.platform_provider

    def provider(self, platform: Platform) -> PlatformProvider:
        if platform not in self._providers:
            if self.settings.platform_provider == "mock":
                provider: PlatformProvider = MockPlatformProvider(platform, self.settings)
            elif platform is Platform.youtube:
                provider = YouTubeProvider(self.settings)
            elif platform is Platform.instagram:
                provider = InstagramProvider(self.settings)
            else:
                provider = TikTokProvider(self.settings)
            self._providers[platform] = provider
        return self._providers[platform]

    def set_provider(self, platform: Platform, provider: PlatformProvider) -> None:
        self._providers[platform] = provider

    def platforms(self) -> list[Platform]:
        return list(Platform)

    def redirect_uri(self, platform: Platform) -> str:
        base = self.settings.resolved_oauth_redirect_base_url
        return f"{base}/api/platforms/{platform.value}/oauth/callback"

    def ensure_mock_accounts(self) -> None:
        """Idempotently create one connected account per platform in mock mode."""
        if self.settings.platform_provider != "mock":
            return
        created_ids: list[int] = []
        with self.database.session() as session:
            for platform in Platform:
                existing = session.scalar(
                    select(ConnectedAccount).where(ConnectedAccount.platform == platform.value)
                )
                if existing is not None:
                    continue
                provider = self.provider(platform)
                token = mock_token(platform)
                identity = provider.fetch_identity(token)
                account = ConnectedAccount(
                    platform=platform.value,
                    external_account_id=identity.external_account_id,
                    handle=identity.handle,
                    display_name=identity.display_name,
                    avatar_url=identity.avatar_url,
                    profile_url=identity.profile_url,
                    account_type=identity.account_type,
                    status="connected",
                    granted_scopes_json=json.dumps(list(DEFAULT_PLATFORM_SCOPES.get(platform, []))),
                    token_ref=self.token_store.new_token_ref(),
                    last_error=None,
                )
                session.add(account)
                session.flush()
                created_ids.append(account.id)
                self.token_store.store(str(account.token_ref), token)
        if not created_ids:
            return
        from runway.platforms.sync import SyncService

        sync = SyncService(self.database, self.settings, self)
        for account_id in created_ids:
            try:
                sync.run(account_id, trigger="mock_seed")
            except Exception:  # noqa: BLE001
                logger.exception("mock seed sync failed for account %s", account_id)
            try:
                self._seed_mock_history(account_id)
            except Exception:  # noqa: BLE001
                logger.exception("mock history seed failed for account %s", account_id)

    def _seed_mock_history(self, account_id: int) -> None:
        """Back-date demo snapshots so follower/content trend charts have history."""
        from datetime import timedelta

        from runway.db.models import (
            AccountMetricSnapshot,
            ContentMetricSnapshot,
            PlatformContent,
            utcnow,
        )

        now = utcnow()
        with self.database.session() as session:
            latest = session.scalar(
                select(AccountMetricSnapshot)
                .where(AccountMetricSnapshot.account_id == account_id)
                .order_by(AccountMetricSnapshot.captured_at.desc())
            )
            final_followers = (latest.followers if latest else None) or 4000
            final_views = (latest.total_views if latest else None) or 40000
            final_likes = (latest.total_likes if latest else None) or 2000
            final_content = (latest.content_count if latest else None) or 10
            for k in range(7, -1, -1):
                growth = 0.82 + 0.18 * (7 - k) / 7
                session.add(
                    AccountMetricSnapshot(
                        account_id=account_id,
                        captured_at=now - timedelta(days=7 * k),
                        followers=round(final_followers * growth),
                        following=latest.following if latest else None,
                        content_count=round(final_content * (0.7 + 0.3 * (7 - k) / 7)),
                        total_views=round(final_views * growth),
                        total_likes=round(final_likes * growth),
                        metrics_json="{}",
                    )
                )
            contents = session.scalars(
                select(PlatformContent).where(PlatformContent.account_id == account_id)
            ).all()
            for content in contents:
                views = content.view_count or 0
                likes = content.like_count or 0
                comments = content.comment_count or 0
                for age_days, scale in ((2, 0.8), (1, 0.92), (0, 1.0)):
                    session.add(
                        ContentMetricSnapshot(
                            content_id=content.id,
                            captured_at=now - timedelta(days=age_days),
                            metrics_json=json.dumps(
                                {
                                    "view_count": round(views * scale),
                                    "like_count": round(likes * scale),
                                    "comment_count": round(comments * scale),
                                }
                            ),
                        )
                    )
