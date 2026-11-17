from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ConnectedAccount
from runway.platforms.base import AuthorizationExpired, PlatformProvider, TokenSet
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

# Short-lived OAuth access tokens (YouTube, TikTok) are refreshed just before
# they expire, from their refresh token.
TOKEN_REFRESH_WINDOW = timedelta(minutes=5)
# Instagram long-lived tokens last 60 days and have no refresh token: they can
# only be refreshed while still valid (and once at least 24 hours old). A
# five-minute window would almost never coincide with a sync, so refresh once
# half of the lifetime has passed; any sync in the second half keeps it alive.
INSTAGRAM_TOKEN_REFRESH_WINDOW = timedelta(days=30)


def token_refresh_window(platform: Platform) -> timedelta:
    if platform is Platform.instagram:
        return INSTAGRAM_TOKEN_REFRESH_WINDOW
    return TOKEN_REFRESH_WINDOW


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

    def fresh_token(self, platform: Platform, token_ref: str) -> TokenSet:
        """Load an account token, refreshing and re-storing it when it is near expiry.

        Raises AuthorizationExpired only when the account must be re-authorized
        (no stored token, a revoked grant). Transient refresh failures (timeouts,
        5xx, rate limits) propagate as their own ProviderError so callers do not
        mistake a network blip for a revoked account.
        """
        token = self.token_store.load(token_ref)
        if token is None:
            raise AuthorizationExpired("account token is missing", status=401)
        if token.expires_at is None:
            return token
        expires_at = token.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at - datetime.now(UTC) > token_refresh_window(platform):
            return token
        refreshed = self.provider(platform).refresh(token)
        self.token_store.store(token_ref, refreshed)
        return refreshed

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
        pending_tokens: list[tuple[str, TokenSet]] = []
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
                pending_tokens.append((str(account.token_ref), token))
        # Token files are written only after the account rows commit, so a
        # failed commit never leaves orphaned secrets on disk.
        for token_ref, token in pending_tokens:
            self.token_store.store(token_ref, token)
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
