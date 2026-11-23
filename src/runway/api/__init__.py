"""Runway HTTP API.

`app` and `create_app` are loaded on first use, so importing a submodule such
as `runway.api.v1.pagination` (for example from the worker) does not build the
application. `uvicorn runway.api.app:app` is unaffected.
"""

from __future__ import annotations

from typing import Any

__all__ = ["app", "create_app"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from runway.api import app as app_module

        return getattr(app_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
