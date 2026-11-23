"""Publisher adapters by name (spec 03 section 5).

`build_publisher_registry` is the single place adapters are registered:

    registry.register(MyPublisher(...))

`assisted` is always registered. Mock publishers are registered only with
`RUNWAY_PLATFORM_PROVIDER=mock`, under the direct adapters' names, and never
alongside live ones (mock is a mode, never a fallback; invariant 7).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import datetime

from runway.config import Settings
from runway.db.base import Database
from runway.db.models import utcnow
from runway.platforms.publisher import Publisher, adapter_kind


class DuplicatePublisher(ValueError):
    pass


class PublisherRegistry:
    def __init__(self, *, mock_mode: bool = False) -> None:
        self._publishers: dict[str, Publisher] = {}
        self.mock_mode = mock_mode

    def register(self, publisher: Publisher) -> Publisher:
        if not isinstance(publisher, Publisher):
            raise TypeError(f"{publisher!r} does not implement the Publisher port")
        if publisher.name in self._publishers:
            raise DuplicatePublisher(f"a publisher named {publisher.name!r} is already registered")
        if adapter_kind(publisher) == "mock" and not self.mock_mode:
            raise ValueError("mock publishers are only registered in mock mode")
        self._publishers[publisher.name] = publisher
        return publisher

    def get(self, name: str | None) -> Publisher | None:
        return self._publishers.get(name) if name else None

    def require(self, name: str) -> Publisher:
        publisher = self.get(name)
        if publisher is None:
            raise LookupError(f"no publisher named {name!r} in this build")
        return publisher

    def names(self) -> frozenset[str]:
        return frozenset(self._publishers)

    def supporting(self, post_type: str) -> list[Publisher]:
        return [publisher for publisher in self if publisher.supports(post_type)]

    def __iter__(self) -> Iterator[Publisher]:
        return iter(self._publishers.values())

    def __contains__(self, name: object) -> bool:
        return name in self._publishers


def build_publisher_registry(
    settings: Settings,
    database: Database,
    *,
    clock: Callable[[], datetime] = utcnow,
) -> PublisherRegistry:
    # Imported here so the port modules stay importable without the adapters.
    from runway.platforms.assisted_publisher import AssistedPublisher

    mock_mode = settings.platform_provider == "mock"
    registry = PublisherRegistry(mock_mode=mock_mode)
    registry.register(AssistedPublisher(database))
    if mock_mode:
        from runway.platforms.mock_publishers import mock_publishers

        for publisher in mock_publishers(settings, clock=clock):
            registry.register(publisher)
        return registry
    # Live direct adapters (youtube_direct, youtube_post, instagram_direct,
    # tiktok_direct, tiktok_inbox) register here as they are built, e.g.:
    #     registry.register(YouTubeDirectPublisher(settings, database))
    return registry
