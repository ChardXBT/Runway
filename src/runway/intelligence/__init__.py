from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from runway.intelligence.profile import StyleProfileService
    from runway.intelligence.retrieval import RetrievalService

__all__ = ["RetrievalService", "StyleProfileService"]


def __getattr__(name: str) -> object:
    if name == "RetrievalService":
        from runway.intelligence.retrieval import RetrievalService

        return RetrievalService
    if name == "StyleProfileService":
        from runway.intelligence.profile import StyleProfileService

        return StyleProfileService
    raise AttributeError(name)
