"""Provider-neutral identity events, mirrored into Runway's tables by `IdentityMirror`."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class UserChanged:
    user_id: str
    email: str | None
    display_name: str | None

    @property
    def subject_key(self) -> str:
        return f"user:{self.user_id}"


@dataclass(frozen=True, slots=True)
class UserRemoved:
    user_id: str

    @property
    def subject_key(self) -> str:
        return f"user:{self.user_id}"


@dataclass(frozen=True, slots=True)
class OrganizationChanged:
    organization_id: str
    name: str | None
    slug: str | None

    @property
    def subject_key(self) -> str:
        return f"org:{self.organization_id}"


@dataclass(frozen=True, slots=True)
class OrganizationRemoved:
    organization_id: str

    @property
    def subject_key(self) -> str:
        return f"org:{self.organization_id}"


@dataclass(frozen=True, slots=True)
class MembershipChanged:
    organization_id: str
    user_id: str
    organization_name: str | None = None
    organization_slug: str | None = None
    email: str | None = None
    display_name: str | None = None
    provider_role: str | None = None

    @property
    def subject_key(self) -> str:
        return f"membership:{self.organization_id}:{self.user_id}"


@dataclass(frozen=True, slots=True)
class MembershipRemoved:
    organization_id: str
    user_id: str

    @property
    def subject_key(self) -> str:
        return f"membership:{self.organization_id}:{self.user_id}"


IdentityEvent = (
    UserChanged
    | UserRemoved
    | OrganizationChanged
    | OrganizationRemoved
    | MembershipChanged
    | MembershipRemoved
)


@dataclass(frozen=True, slots=True)
class ParsedDelivery:
    """One webhook body after parsing. `event` is None for types Runway ignores."""

    event_type: str
    event_at: datetime | None
    event: IdentityEvent | None
