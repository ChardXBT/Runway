"""Translate Clerk webhook payloads into provider-neutral identity events.

Clerk wraps every event as `{"type": ..., "data": {...}, "timestamp": <ms>}`.
Handled types: `user.created|updated|deleted`,
`organization.created|updated|deleted` and
`organizationMembership.created|updated|deleted`. Anything else is accepted and
ignored, so Clerk does not keep retrying it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from runway.identity.events import (
    IdentityEvent,
    MembershipChanged,
    MembershipRemoved,
    OrganizationChanged,
    OrganizationRemoved,
    ParsedDelivery,
    UserChanged,
    UserRemoved,
)


class WebhookPayloadInvalid(ValueError):
    """A correctly signed body that is not a Clerk event Runway can read."""


def _text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _required(data: Mapping[str, Any], key: str) -> str:
    value = _text(data.get(key))
    if value is None:
        raise WebhookPayloadInvalid(f"the event has no {key}")
    return value


def _full_name(first: object, last: object, fallback: object = None) -> str | None:
    name = " ".join(part for part in (_text(first), _text(last)) if part)
    return name or _text(fallback)


def _primary_email(user: Mapping[str, Any]) -> str | None:
    primary_id = user.get("primary_email_address_id")
    addresses = user.get("email_addresses")
    if not isinstance(addresses, list):
        return None
    first: str | None = None
    for address in addresses:
        if not isinstance(address, Mapping):
            continue
        email = _text(address.get("email_address"))
        if email is None:
            continue
        if first is None:
            first = email
        if primary_id is not None and address.get("id") == primary_id:
            return email
    return first


def _event_time(payload: Mapping[str, Any], data: Mapping[str, Any]) -> datetime | None:
    for value in (payload.get("timestamp"), data.get("updated_at")):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            continue
        return datetime.fromtimestamp(value / 1000, UTC)
    return None


def _user_event(kind: str, data: Mapping[str, Any]) -> IdentityEvent:
    user_id = _required(data, "id")
    if kind == "deleted":
        return UserRemoved(user_id=user_id)
    return UserChanged(
        user_id=user_id,
        email=_primary_email(data),
        display_name=_full_name(
            data.get("first_name"), data.get("last_name"), data.get("username")
        ),
    )


def _organization_event(kind: str, data: Mapping[str, Any]) -> IdentityEvent:
    organization_id = _required(data, "id")
    if kind == "deleted":
        return OrganizationRemoved(organization_id=organization_id)
    return OrganizationChanged(
        organization_id=organization_id,
        name=_text(data.get("name")),
        slug=_text(data.get("slug")),
    )


def _membership_event(kind: str, data: Mapping[str, Any]) -> IdentityEvent:
    organization = data.get("organization")
    member = data.get("public_user_data")
    if not isinstance(organization, Mapping) or not isinstance(member, Mapping):
        raise WebhookPayloadInvalid("the membership event has no organization or user")
    organization_id = _required(organization, "id")
    user_id = _required(member, "user_id")
    if kind == "deleted":
        return MembershipRemoved(organization_id=organization_id, user_id=user_id)
    role = _text(data.get("role"))
    return MembershipChanged(
        organization_id=organization_id,
        user_id=user_id,
        organization_name=_text(organization.get("name")),
        organization_slug=_text(organization.get("slug")),
        email=_text(member.get("identifier")),
        display_name=_full_name(member.get("first_name"), member.get("last_name")),
        provider_role=role.removeprefix("org:") if role else None,
    )


_PARSERS = {
    "user": _user_event,
    "organization": _organization_event,
    "organizationMembership": _membership_event,
}
_KINDS = frozenset({"created", "updated", "deleted"})


def parse_clerk_event(payload: object) -> ParsedDelivery:
    if not isinstance(payload, Mapping):
        raise WebhookPayloadInvalid("the body is not a JSON object")
    event_type = _text(payload.get("type"))
    if event_type is None:
        raise WebhookPayloadInvalid("the event has no type")
    if len(event_type) > 80:
        raise WebhookPayloadInvalid("the event type is too long")
    family, _, kind = event_type.rpartition(".")
    parser = _PARSERS.get(family)
    if parser is None or kind not in _KINDS:
        return ParsedDelivery(event_type=event_type, event_at=None, event=None)
    data = payload.get("data")
    if not isinstance(data, Mapping):
        raise WebhookPayloadInvalid("the event has no data object")
    return ParsedDelivery(
        event_type=event_type,
        event_at=_event_time(payload, data),
        event=parser(kind, data),
    )
