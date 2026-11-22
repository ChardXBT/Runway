"""Choose the identity provider named by the `identity_provider` setting."""

from __future__ import annotations

from collections.abc import Callable

from runway.config import Settings
from runway.config.settings import SUPPORTED_IDENTITY_PROVIDERS
from runway.identity.base import IdentityProvider
from runway.identity.clerk import PROVIDER_NAME as CLERK
from runway.identity.clerk import ClerkConfig, ClerkIdentityProvider
from runway.identity.jwt import JwksFetcher, httpx_jwks_fetcher, normalized_origins

ProviderFactory = Callable[[Settings, JwksFetcher], IdentityProvider]


def _clerk(settings: Settings, fetcher: JwksFetcher) -> IdentityProvider:
    config = ClerkConfig(
        issuer=settings.clerk_issuer.strip(),
        jwks_url=settings.clerk_jwks_endpoint,
        authorized_parties=normalized_origins(settings.clerk_authorized_party_list),
        audience=settings.clerk_audience.strip() or None,
        leeway_seconds=settings.identity_token_leeway_seconds,
    )
    return ClerkIdentityProvider(config, fetcher=fetcher)


PROVIDERS: dict[str, ProviderFactory] = {CLERK: _clerk}

if set(PROVIDERS) != SUPPORTED_IDENTITY_PROVIDERS:  # pragma: no cover - import-time guard
    raise RuntimeError("runway.identity.registry and SUPPORTED_IDENTITY_PROVIDERS disagree")


def build_identity_provider(
    settings: Settings, *, fetcher: JwksFetcher = httpx_jwks_fetcher
) -> IdentityProvider | None:
    """Return the configured provider, or None when none is configured.

    Building a provider performs no network call; keys are fetched on first use.
    An unknown name raises, although `Settings` already refuses one in `hosted`.
    """
    name = settings.identity_provider.strip()
    if not name:
        return None
    factory = PROVIDERS.get(name)
    if factory is None:
        raise ValueError(f"unknown identity provider {name!r}")
    return factory(settings, fetcher)
