"""Add the hosted identity mirror (organization links, webhook deliveries) and workspace limits.

Revision ID: 0016h_hosted_identity
Revises: 0013_workspaces

Additive only: three new tables, no change to existing rows. On a fresh
database 0001 already created them from the models (alembic/env.py imports
runway.db.hosted_models), so only missing tables are created.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

revision = "0016h_hosted_identity"
down_revision = "0013_workspaces"
branch_labels = None
depends_on = None

NEW_TABLES = ("workspace_identity_links", "identity_webhook_deliveries", "workspace_limits")


def _table_names() -> set[str] | None:
    """Existing tables, or None when generating SQL offline (nothing can be read)."""
    if context.is_offline_mode():
        return None
    return set(sa.inspect(op.get_bind()).get_table_names())


def _missing(table: str, existing: set[str] | None) -> bool:
    return existing is None or table not in existing


def upgrade() -> None:
    existing = _table_names()
    if _missing("workspace_identity_links", existing):
        op.create_table(
            "workspace_identity_links",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("workspace_id", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(length=40), nullable=False),
            sa.Column("external_org_id", sa.String(length=200), nullable=False),
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("external_deleted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["workspaces.id"], name="fk_workspace_identity_links_workspace"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("provider", "external_org_id", name="uq_workspace_identity_org"),
            sa.UniqueConstraint("workspace_id", name="uq_workspace_identity_workspace"),
        )
    if _missing("identity_webhook_deliveries", existing):
        op.create_table(
            "identity_webhook_deliveries",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(length=40), nullable=False),
            sa.Column("delivery_id", sa.String(length=200), nullable=False),
            sa.Column("event_type", sa.String(length=80), nullable=False),
            sa.Column("subject_key", sa.String(length=420), nullable=True),
            sa.Column("event_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("outcome", sa.String(length=20), nullable=False),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("provider", "delivery_id", name="uq_identity_webhook_delivery"),
        )
        op.create_index(
            "ix_identity_webhook_subject",
            "identity_webhook_deliveries",
            ["provider", "subject_key", "event_at"],
        )
    if _missing("workspace_limits", existing):
        op.create_table(
            "workspace_limits",
            sa.Column("workspace_id", sa.Integer(), nullable=False),
            sa.Column("storage_bytes", sa.BigInteger(), nullable=True),
            sa.Column("connected_accounts", sa.Integer(), nullable=True),
            sa.Column("posts_per_day", sa.Integer(), nullable=True),
            sa.Column("api_requests_per_minute", sa.Integer(), nullable=True),
            sa.Column(
                "publishing_disabled", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("publishing_disabled_reason", sa.Text(), nullable=True),
            sa.Column("updated_by", sa.String(length=200), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["workspaces.id"], name="fk_workspace_limits_workspace"
            ),
            sa.PrimaryKeyConstraint("workspace_id"),
        )


def downgrade() -> None:
    existing = _table_names()
    for table_name in reversed(NEW_TABLES):
        if existing is None or table_name in existing:
            op.drop_table(table_name)
