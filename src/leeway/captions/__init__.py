from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from leeway.captions.service import CaptionService

__all__ = ["CaptionService"]


def __getattr__(name: str) -> object:
    if name == "CaptionService":
        from leeway.captions.service import CaptionService

        return CaptionService
    raise AttributeError(name)
