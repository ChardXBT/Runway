"""Untrusted platform text in agent-facing results (spec 06 section 5).

Text that came from a platform (comment text, captions, titles, usernames,
platform error messages) is data, never instructions. Every such value is
returned inside a wrapper object::

    {"untrusted": true, "source": "platform", "text": "..."}

and the server's tool descriptions and guidance never quote it. The keys are
listed once here; `wrap_untrusted` walks any JSON-ready result and wraps the
string values found under them, at any depth.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Keys whose string values may carry text a platform user or the platform wrote.
UNTRUSTED_KEYS: frozenset[str] = frozenset(
    {
        # Comments and authors.
        "text",
        "author_name",
        "author_external_id",
        # Content and accounts.
        "title",
        "caption",
        "description",
        "handle",
        "display_name",
        "username",
        # Platform-originated errors and remote titles (see runway.agent.services).
        "error",
        "last_error",
        "error_detail",
        "error_summary",
        "state_reason",
        "platform_message",
        "remote_title",
    }
)
UNTRUSTED_MARKER = "untrusted"


def untrusted_text(value: str) -> dict[str, Any]:
    return {UNTRUSTED_MARKER: True, "source": "platform", "text": value}


def is_untrusted_wrapper(value: object) -> bool:
    return isinstance(value, Mapping) and value.get(UNTRUSTED_MARKER) is True


def wrap_untrusted(value: Any, keys: frozenset[str] = UNTRUSTED_KEYS) -> Any:
    """A copy of `value` with every string under an untrusted key wrapped."""
    if isinstance(value, Mapping):
        if is_untrusted_wrapper(value):
            return dict(value)
        wrapped: dict[str, Any] = {}
        for key, item in value.items():
            if key in keys and isinstance(item, str):
                wrapped[key] = untrusted_text(item)
            else:
                wrapped[key] = wrap_untrusted(item, keys)
        return wrapped
    if isinstance(value, list | tuple):
        return [wrap_untrusted(item, keys) for item in value]
    return value
