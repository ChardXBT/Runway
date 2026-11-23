"""Builds the posts service for the API from settings (the worker uses `posts.engine`)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import ConnectedAccount, utcnow
from runway.platforms.publisher_registry import PublisherRegistry, build_publisher_registry
from runway.posts.media import default_media_stores, probe_media_validator
from runway.posts.service import PostService
from runway.posts.snapshot import TokenProvider


def provider_token_provider(database: Database, settings: Settings) -> TokenProvider:
    """Fresh tokens through the provider registry (refreshed under the account lock)."""
    from runway.platforms.capabilities import Platform
    from runway.platforms.registry import ProviderRegistry

    registry = ProviderRegistry(database, settings)

    def provide(account: ConnectedAccount) -> Any:
        if not account.token_ref:
            raise RuntimeError("the account has no stored token")
        return registry.fresh_token(Platform(account.platform), account.token_ref)

    return provide


def build_post_service(
    settings: Settings,
    database: Database,
    *,
    registry: PublisherRegistry | None = None,
    clock: Callable[[], datetime] = utcnow,
) -> PostService:
    return PostService(
        database=database,
        settings=settings,
        registry=registry or build_publisher_registry(settings, database, clock=clock),
        clock=clock,
        media_validator=probe_media_validator,
        token_provider=provider_token_provider(database, settings),
        media_stores=dict(default_media_stores(settings)),
    )

