"""Mirror identity-provider users, organizations and memberships into Runway's tables.

Runway's `workspaces` and `workspace_members` are the system of record for
workspaces and roles (spec 05 section 2; research/recheck-20261122 section 5).
The provider supplies only who a user is and which organizations they belong
to. The rules:

* An organization maps to one workspace through `workspace_identity_links`;
  the workspace is created the first time the organization is seen.
* The first member of a new organization's workspace becomes its `owner`,
  provided the provider reports them as an organization admin (Clerk's creator
  of an organization is its admin). The claim is taken once, atomically, and
  is closed for good as soon as the workspace has any owner. A user whose
  membership in the workspace was ever revoked can never take it, so a removed
  member holding a still-valid admin token cannot come back as owner. Every
  later member joins with the configured default role.
* Membership rows are created only by that first-owner claim or by the
  provider's membership webhooks. A signed-in user without a row gets 403.
* Removing a membership, a user or an organization revokes membership rows.
  Workspace data is never deleted here; deletion is a separate flow.
* A provider role change never changes a Runway role.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast, get_args

from sqlalchemy import CursorResult, func, select, update
from sqlalchemy.orm import Session

from runway.auth.principal import Role
from runway.db.hosted_models import IdentityWebhookDelivery, WorkspaceIdentityLink
from runway.db.models import AuditLogEntry, User, Workspace, WorkspaceMember
from runway.identity.base import ExternalIdentity
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

logger = logging.getLogger(__name__)

ROLES: frozenset[str] = frozenset(get_args(Role))
PROVIDER_ADMIN_ROLE = "admin"
# Column sizes of users.email and users.display_name: a longer value would
# raise DataError on Postgres and turn a sign-in or webhook into a 500.
EMAIL_MAX = 320
DISPLAY_NAME_MAX = 200
MEMBER_REVOKED = "identity.member_revoked"

Outcome = str  # "applied" | "duplicate" | "stale" | "ignored"


class MembershipRefused(Exception):
    """The caller is authenticated but has no usable membership (403)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


@dataclass(frozen=True, slots=True)
class ResolvedMember:
    user_id: int
    workspace_id: int
    role: Role
    display: str


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _slug_base(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug[:60] or "workspace"


class IdentityMirror:
    def __init__(
        self,
        provider: str,
        *,
        default_role: Role = "viewer",
        clock: Callable[[], datetime] = _utcnow,
    ) -> None:
        if default_role == "owner":
            raise ValueError("the default member role cannot be owner")
        self.provider = provider
        self.default_role = default_role
        self.clock = clock

    # ------------------------------------------------------------ sign-in path

    def resolve(self, session: Session, identity: ExternalIdentity) -> ResolvedMember:
        """Mirror the signed-in user and return their Runway membership, or refuse."""
        user = self._user(session, identity.user_id, identity.email, identity.display_name)
        if identity.organization_id is None:
            raise MembershipRefused(
                "workspace_required",
                "choose an organization in the web app; each organization is a workspace",
            )
        link = self._link(
            session,
            identity.organization_id,
            name=identity.organization_name,
            slug=identity.organization_slug,
        )
        workspace = self._active_workspace(session, link)
        member = self._member(session, workspace.id, user.id)
        if member is None:
            if identity.organization_role == PROVIDER_ADMIN_ROLE and self._claim(
                session, link, user.id
            ):
                member = self._add_member(session, workspace.id, user, "owner", via="sign-in")
            else:
                raise MembershipRefused(
                    "membership_unknown",
                    "Runway has no membership for you in this workspace yet; "
                    "ask a workspace owner, or try again in a minute",
                )
        if member.role not in ROLES:
            raise MembershipRefused("membership_invalid", "your membership has an unknown role")
        return ResolvedMember(
            user_id=user.id,
            workspace_id=workspace.id,
            role=cast(Role, member.role),
            display=user.display_name or user.email or identity.user_id,
        )

    def _active_workspace(self, session: Session, link: WorkspaceIdentityLink) -> Workspace:
        workspace = session.get(Workspace, link.workspace_id)
        if link.external_deleted_at is not None or workspace is None or workspace.deleted_at:
            raise MembershipRefused("workspace_unavailable", "this workspace is not available")
        return workspace

    # ------------------------------------------------------------ webhook path

    def apply(self, session: Session, parsed: ParsedDelivery, delivery_id: str) -> Outcome:
        """Apply one verified delivery once. Replays and older events are recorded, not applied."""
        existing = session.scalar(
            select(IdentityWebhookDelivery.id).where(
                IdentityWebhookDelivery.provider == self.provider,
                IdentityWebhookDelivery.delivery_id == delivery_id,
            )
        )
        if existing is not None:
            return "duplicate"
        event = parsed.event
        subject_key = event.subject_key if event is not None else None
        event_at = _aware(parsed.event_at)
        if event is None:
            outcome: Outcome = "ignored"
        elif self._is_stale(session, subject_key, event_at):
            outcome = "stale"
        else:
            outcome = self._apply_event(session, event)
        session.add(
            IdentityWebhookDelivery(
                provider=self.provider,
                delivery_id=delivery_id,
                event_type=parsed.event_type,
                subject_key=subject_key,
                event_at=event_at,
                outcome=outcome,
                received_at=self.clock(),
            )
        )
        session.flush()
        return outcome

    def _is_stale(
        self, session: Session, subject_key: str | None, event_at: datetime | None
    ) -> bool:
        if subject_key is None or event_at is None:
            return False
        newest = session.scalar(
            select(func.max(IdentityWebhookDelivery.event_at)).where(
                IdentityWebhookDelivery.provider == self.provider,
                IdentityWebhookDelivery.subject_key == subject_key,
                IdentityWebhookDelivery.outcome == "applied",
            )
        )
        newest_at = _aware(newest)
        return newest_at is not None and newest_at > event_at

    def _apply_event(self, session: Session, event: IdentityEvent) -> Outcome:
        if isinstance(event, UserChanged):
            self._user(session, event.user_id, event.email, event.display_name)
        elif isinstance(event, UserRemoved):
            self._remove_user_memberships(session, event.user_id)
        elif isinstance(event, OrganizationChanged):
            self._link(session, event.organization_id, name=event.name, slug=event.slug)
        elif isinstance(event, OrganizationRemoved):
            self._remove_organization(session, event.organization_id)
        elif isinstance(event, MembershipChanged):
            return self._membership_changed(session, event)
        elif isinstance(event, MembershipRemoved):
            self._membership_removed(session, event.organization_id, event.user_id)
        return "applied"

    def _membership_changed(self, session: Session, event: MembershipChanged) -> Outcome:
        link = self._link(
            session,
            event.organization_id,
            name=event.organization_name,
            slug=event.organization_slug,
        )
        if link.external_deleted_at is not None:
            return "ignored"
        user = self._user(session, event.user_id, event.email, event.display_name)
        if self._member(session, link.workspace_id, user.id) is not None:
            # Runway's role is the system of record; a provider role change is ignored.
            return "applied"
        role: Role = self.default_role
        if event.provider_role == PROVIDER_ADMIN_ROLE and self._claim(session, link, user.id):
            role = "owner"
        self._add_member(session, link.workspace_id, user, role, via="webhook")
        return "applied"

    def _membership_removed(self, session: Session, organization_id: str, user_id: str) -> None:
        link = self._find_link(session, organization_id)
        user = session.scalar(select(User).where(User.external_id == user_id))
        if link is None or user is None:
            return
        member = self._member(session, link.workspace_id, user.id)
        if member is not None:
            self._revoke(session, member, reason="membership_removed")

    def _remove_user_memberships(self, session: Session, user_id: str) -> None:
        user = session.scalar(select(User).where(User.external_id == user_id))
        if user is None:
            return
        for member in session.scalars(
            select(WorkspaceMember).where(WorkspaceMember.user_id == user.id)
        ).all():
            self._revoke(session, member, reason="user_removed")

    def _remove_organization(self, session: Session, organization_id: str) -> None:
        link = self._find_link(session, organization_id)
        if link is None:
            return
        if link.external_deleted_at is None:
            link.external_deleted_at = self.clock()
        for member in session.scalars(
            select(WorkspaceMember).where(WorkspaceMember.workspace_id == link.workspace_id)
        ).all():
            self._revoke(session, member, reason="organization_removed")
        self._audit(session, link.workspace_id, "identity.organization_removed", None, {})

    # ------------------------------------------------------------ rows

    def _user(
        self, session: Session, external_id: str, email: str | None, display_name: str | None
    ) -> User:
        email = email[:EMAIL_MAX] if email else email
        display_name = display_name[:DISPLAY_NAME_MAX] if display_name else display_name
        user = session.scalar(select(User).where(User.external_id == external_id))
        if user is None:
            user = User(
                external_id=external_id,
                email=email,
                display_name=display_name,
                created_at=self.clock(),
            )
            session.add(user)
            session.flush()
            return user
        if email and user.email != email:
            user.email = email
        if display_name and user.display_name != display_name:
            user.display_name = display_name
        return user

    def _find_link(self, session: Session, organization_id: str) -> WorkspaceIdentityLink | None:
        return session.scalar(
            select(WorkspaceIdentityLink).where(
                WorkspaceIdentityLink.provider == self.provider,
                WorkspaceIdentityLink.external_org_id == organization_id,
            )
        )

    def _link(
        self, session: Session, organization_id: str, *, name: str | None, slug: str | None
    ) -> WorkspaceIdentityLink:
        link = self._find_link(session, organization_id)
        if link is not None:
            workspace = session.get(Workspace, link.workspace_id)
            if workspace is not None and name and workspace.name != name:
                workspace.name = name[:200]
            return link
        now = self.clock()
        workspace = Workspace(
            slug=self._unique_slug(session, slug or name or organization_id, organization_id),
            name=(name or slug or organization_id)[:200],
            profile_note=f"hosted workspace for a {self.provider} organization",
            created_at=now,
        )
        session.add(workspace)
        session.flush()
        link = WorkspaceIdentityLink(
            workspace_id=workspace.id,
            provider=self.provider,
            external_org_id=organization_id,
            created_at=now,
            updated_at=now,
        )
        session.add(link)
        session.flush()
        self._audit(session, workspace.id, "identity.workspace_created", None, {})
        return link

    def _unique_slug(self, session: Session, wanted: str, organization_id: str) -> str:
        base = _slug_base(wanted)
        taken = set(
            session.scalars(select(Workspace.slug).where(Workspace.slug.like(f"{base}%"))).all()
        )
        if base not in taken:
            return base
        suffix = hashlib.sha256(organization_id.encode("utf-8")).hexdigest()[:8]
        candidate = f"{base}-{suffix}"
        counter = 2
        while candidate in taken:
            candidate = f"{base}-{suffix}-{counter}"
            counter += 1
        return candidate

    def _claim(self, session: Session, link: WorkspaceIdentityLink, user_id: int) -> bool:
        """Take the one-time first-owner claim; True only for the caller that took it."""
        now = self.clock()
        if link.claimed_at is None and self._has_owner(session, link.workspace_id):
            # An owner exists (for example promoted by an owner while the claim
            # was open): close the claim so nobody else can take it later.
            self._close_claim(session, link, now)
            return False
        if self._was_revoked(session, link.workspace_id, user_id):
            return False
        result = cast(
            CursorResult[object],
            session.execute(
                update(WorkspaceIdentityLink)
                .where(
                    WorkspaceIdentityLink.id == link.id,
                    WorkspaceIdentityLink.claimed_at.is_(None),
                )
                .values(claimed_at=now, updated_at=now)
                .execution_options(synchronize_session=False)
            ),
        )
        claimed = result.rowcount == 1
        session.refresh(link)
        return claimed

    @staticmethod
    def _close_claim(session: Session, link: WorkspaceIdentityLink, now: datetime) -> None:
        session.execute(
            update(WorkspaceIdentityLink)
            .where(
                WorkspaceIdentityLink.id == link.id,
                WorkspaceIdentityLink.claimed_at.is_(None),
            )
            .values(claimed_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        session.refresh(link)

    @staticmethod
    def _was_revoked(session: Session, workspace_id: int, user_id: int) -> bool:
        """A revocation is a tombstone: the audit log keeps it for good."""
        return (
            session.scalar(
                select(AuditLogEntry.id).where(
                    AuditLogEntry.workspace_id == workspace_id,
                    AuditLogEntry.action == MEMBER_REVOKED,
                    AuditLogEntry.target_type == "user",
                    AuditLogEntry.target_id == str(user_id),
                )
            )
            is not None
        )

    @staticmethod
    def _member(session: Session, workspace_id: int, user_id: int) -> WorkspaceMember | None:
        return session.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user_id,
            )
        )

    def _add_member(
        self, session: Session, workspace_id: int, user: User, role: Role, *, via: str
    ) -> WorkspaceMember:
        member = WorkspaceMember(
            workspace_id=workspace_id, user_id=user.id, role=role, created_at=self.clock()
        )
        session.add(member)
        session.flush()
        self._audit(
            session, workspace_id, "identity.member_added", user.id, {"role": role, "via": via}
        )
        return member

    def _revoke(self, session: Session, member: WorkspaceMember, *, reason: str) -> None:
        workspace_id, user_id, role = member.workspace_id, member.user_id, member.role
        session.delete(member)
        session.flush()
        detail: dict[str, object] = {"role": role, "reason": reason}
        if role == "owner" and not self._has_owner(session, workspace_id):
            detail["last_owner_removed"] = True
            logger.warning(
                "workspace %s has no owner after an identity-provider removal",
                workspace_id,
                extra={"event_type": "identity_last_owner_removed", "workspace_id": workspace_id},
            )
        self._audit(session, workspace_id, MEMBER_REVOKED, user_id, detail)

    @staticmethod
    def _has_owner(session: Session, workspace_id: int) -> bool:
        return (
            session.scalar(
                select(WorkspaceMember.id).where(
                    WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.role == "owner"
                )
            )
            is not None
        )

    def _audit(
        self,
        session: Session,
        workspace_id: int,
        action: str,
        user_id: int | None,
        detail: dict[str, object],
    ) -> None:
        session.add(
            AuditLogEntry(
                workspace_id=workspace_id,
                actor_kind="system",
                actor_id=f"identity:{self.provider}",
                action=action,
                target_type="user" if user_id is not None else "workspace",
                target_id=str(user_id if user_id is not None else workspace_id),
                detail_json=json.dumps(detail, sort_keys=True),
                created_at=self.clock(),
            )
        )
