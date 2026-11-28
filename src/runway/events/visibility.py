"""Which events a principal may read, by its scopes (spec 06 section 8).

An event is visible when the principal holds any scope listed for the event
type's family (the part before the first dot). Families without an entry are
visible to `workspace:admin` only, so a new event family is private until
someone decides who may read it.
"""

from __future__ import annotations

from collections.abc import Callable

from runway.auth.principal import (
    ACCOUNTS_READ,
    CONTENT_READ,
    INBOX_READ,
    MEDIA_WRITE,
    POSTS_READ,
    WORKSPACE_ADMIN,
    Principal,
)

EVENT_FAMILY_SCOPES: dict[str, frozenset[str]] = {
    "account": frozenset({ACCOUNTS_READ}),
    # Sync status and quota are part of "Library, analytics, sync status".
    "sync": frozenset({CONTENT_READ, ACCOUNTS_READ}),
    "quota": frozenset({CONTENT_READ, ACCOUNTS_READ}),
    "content": frozenset({CONTENT_READ}),
    "comment": frozenset({INBOX_READ}),
    "target": frozenset({POSTS_READ}),
    "post": frozenset({POSTS_READ}),
    "media": frozenset({MEDIA_WRITE, POSTS_READ}),
    "proposal": frozenset({POSTS_READ, CONTENT_READ}),
}
_ADMIN_ONLY = frozenset({WORKSPACE_ADMIN})


def scopes_for_event(event_type: str) -> frozenset[str]:
    family = event_type.split(".", 1)[0]
    return EVENT_FAMILY_SCOPES.get(family, _ADMIN_ONLY)


def event_visible(scopes: frozenset[str], event_type: str) -> bool:
    return not scopes.isdisjoint(scopes_for_event(event_type))


def visibility_for(principal: Principal) -> Callable[[str], bool]:
    scopes = principal.scopes

    def visible(event_type: str) -> bool:
        return event_visible(scopes, event_type)

    return visible
