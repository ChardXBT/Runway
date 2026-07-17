from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from leeway.intelligence.profile import StyleProfileService
    from leeway.intelligence.retrieval import RetrievalService

__all__ = ["RetrievalService", "StyleProfileService"]


def __getattr__(name: str) -> object:
    if name == "RetrievalService":
        from leeway.intelligence.retrieval import RetrievalService

        return RetrievalService
    if name == "StyleProfileService":
        from leeway.intelligence.profile import StyleProfileService

        return StyleProfileService
    raise AttributeError(name)
