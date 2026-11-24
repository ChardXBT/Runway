"""Tables for the hosted profile's identity mirror and operator limits (migration 0016h).

Kept apart from `runway.db.models` so the hosted work adds a module instead of
editing the shared one. `alembic/env.py` imports this module, so a fresh
database built by 0001 (`create_all`) and an upgraded one (0016h) end up with
the same tables.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from runway.db.base import Base
from runway.db.models import WorkspaceScoped, utcnow


class WorkspaceIdentityLink(Base):
    """Maps an identity-provider organization to a Runway workspace.

    `claimed_at` is set once, when the workspace's first member is made its
    owner; later members join with the default role. `external_deleted_at`
    records that the provider deleted the organization: memberships are
    revoked, the workspace's data is kept for the separate deletion flow.
    """

    __tablename__ = "workspace_identity_links"
    __table_args__ = (
        UniqueConstraint("provider", "external_org_id", name="uq_workspace_identity_org"),
        UniqueConstraint("workspace_id", name="uq_workspace_identity_workspace"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", name="fk_workspace_identity_links_workspace")
    )
    provider: Mapped[str] = mapped_column(String(40))
    external_org_id: Mapped[str] = mapped_column(String(200))
    claimed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class IdentityWebhookDelivery(Base):
    """One identity-provider webhook delivery, kept for de-duplication and ordering.

    `delivery_id` is the provider's message id (Svix `svix-id`); a replay of
    the same id is answered without being applied again. `subject_key` and
    `event_at` let an older event for the same user, organization or
    membership be skipped when a newer one has already been applied.
    """

    __tablename__ = "identity_webhook_deliveries"
    __table_args__ = (
        UniqueConstraint("provider", "delivery_id", name="uq_identity_webhook_delivery"),
        Index("ix_identity_webhook_subject", "provider", "subject_key", "event_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    provider: Mapped[str] = mapped_column(String(40))
    delivery_id: Mapped[str] = mapped_column(String(200))
    event_type: Mapped[str] = mapped_column(String(80))
    subject_key: Mapped[str | None] = mapped_column(String(420))
    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(20))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class WorkspaceLimits(Base, WorkspaceScoped):
    """Operator overrides for one workspace: quotas and the publishing kill switch.

    A NULL quota column means "use the configured default". The row is
    optional; a workspace without one gets the defaults and may publish.
    """

    __tablename__ = "workspace_limits"

    workspace_id: Mapped[int] = mapped_column(
        ForeignKey("workspaces.id", name="fk_workspace_limits_workspace"), primary_key=True
    )
    storage_bytes: Mapped[int | None] = mapped_column(BigInteger)
    connected_accounts: Mapped[int | None] = mapped_column(Integer)
    posts_per_day: Mapped[int | None] = mapped_column(Integer)
    api_requests_per_minute: Mapped[int | None] = mapped_column(Integer)
    publishing_disabled: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    publishing_disabled_reason: Mapped[str | None] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(String(200))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
