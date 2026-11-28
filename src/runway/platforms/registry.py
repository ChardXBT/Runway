from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass
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
from runway.platforms.tokens import TokenStore, TokenStoreError
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


TOKEN_RECOVERY_DETAIL = "stored OAuth token is missing or unreadable; reconnect the account"


@dataclass(frozen=True)
class TokenReconciliationResult:
    accounts_marked_needs_reauth: int
    orphaned_tokens_removed: int


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
        # The refresh decision, provider refresh, and replacement write are one
        # cross-process critical section. A process that waits becomes the
        # loser only after the winner has stored its replacement, so it reloads
        # that token instead of consuming a second provider refresh grant.
        with self.token_store.locked():
            try:
                token = self.token_store.load(token_ref)
            except TokenStoreError as exc:
                raise AuthorizationExpired(TOKEN_RECOVERY_DETAIL, status=401) from exc
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

    def _account_token_ref(self, account_id: int) -> str | None:
        with self.database.session() as session:
            account = session.get(ConnectedAccount, account_id)
            return str(account.token_ref) if account is not None and account.token_ref else None

    def fresh_account_token(self, platform: Platform, account_id: int) -> TokenSet:
        """Load a fresh token even if a reconnect swaps the account's token ref.

        Sync and inbox work capture account state before network work. A
        reconnect may commit a fresh token ref and remove the old one in that
        gap; treating the stale ref as a revoked grant would incorrectly mark a
        healthy reconnected account as needing reauth. Retry once with the
        row's current ref, while preserving the original failure when the ref
        is unchanged or the account has been disconnected.
        """
        token_ref = self._account_token_ref(account_id)
        if token_ref is None:
            raise AuthorizationExpired("account has no stored token", status=401)
        for _ in range(2):
            try:
                return self.fresh_token(platform, token_ref)
            except AuthorizationExpired:
                current_ref = self._account_token_ref(account_id)
                if current_ref is None or current_ref == token_ref:
                    raise
                token_ref = current_ref
        raise AssertionError("unreachable")

    def reconcile_connected_accounts(self) -> TokenReconciliationResult:
        """Reconcile account rows with the encrypted token blob.

        A connected row without decryptable token bytes is recovery state, not a
        ready account: mark it ``needs_reauth`` with a secret-free detail. Token
        refs left behind by an interrupted connect are unreferenced encrypted
        blobs and can be removed while the cross-process token lock is held.
        """
        marked = 0
        with self.token_store.locked():
            with self.database.session() as session:
                accounts = session.scalars(select(ConnectedAccount)).all()
                keep_refs = {str(account.token_ref) for account in accounts if account.token_ref}
                for account in accounts:
                    if account.status != "connected":
                        continue
                    detail: str | None = None
                    if not account.token_ref:
                        detail = TOKEN_RECOVERY_DETAIL
                    else:
                        try:
                            stored = self.token_store.load(str(account.token_ref))
                        except TokenStoreError:
                            detail = TOKEN_RECOVERY_DETAIL
                        else:
                            if stored is None:
                                detail = TOKEN_RECOVERY_DETAIL
                    if detail is not None:
                        account.status = "needs_reauth"
                        account.last_error = detail
                        marked += 1
            try:
                pruned = self.token_store.prune_except(keep_refs)
            except TokenStoreError:
                logger.warning("unreferenced platform token cleanup failed", exc_info=True)
                pruned = 0
        return TokenReconciliationResult(
            accounts_marked_needs_reauth=marked,
            orphaned_tokens_removed=pruned,
        )

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
        written_token_refs: list[str] = []
        try:
            # Token bytes are written before the rows that reference them. A
            # crash can leave only an encrypted orphan (which reconciliation
            # prunes), never a connected row without usable token bytes.
            with self.token_store.locked(), self.database.session() as session:
                for platform in Platform:
                    existing = session.scalar(
                        select(ConnectedAccount).where(ConnectedAccount.platform == platform.value)
                    )
                    if existing is not None:
                        continue
                    provider = self.provider(platform)
                    token = mock_token(platform)
                    identity = provider.fetch_identity(token)
                    token_ref = self.token_store.new_token_ref()
                    self.token_store.store(token_ref, token)
                    written_token_refs.append(token_ref)
                    account = ConnectedAccount(
                        platform=platform.value,
                        external_account_id=identity.external_account_id,
                        handle=identity.handle,
                        display_name=identity.display_name,
                        avatar_url=identity.avatar_url,
                        profile_url=identity.profile_url,
                        account_type=identity.account_type,
                        status="connected",
                        granted_scopes_json=json.dumps(
                            list(DEFAULT_PLATFORM_SCOPES.get(platform, []))
                        ),
                        token_ref=token_ref,
                        last_error=None,
                    )
                    session.add(account)
                    session.flush()
                    created_ids.append(account.id)
        except BaseException:
            for token_ref in written_token_refs:
                with contextlib.suppress(Exception):
                    self.token_store.delete(token_ref)
            raise
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
