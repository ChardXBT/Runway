"""Agent interface: release policy, per-key release grants and outbound webhooks.

Revision ID: 0019_agent_interface
Revises: 0018b_sync_state

Follows the publishing engine (0017). The real-time job's 0018 lands in
parallel; when it merges, the orchestrator re-chains whichever comes later.
Only new tables are added, so the order does not matter for the schema.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

revision = "0019_agent_interface"
down_revision = "0018b_sync_state"
branch_labels = None
depends_on = None

# Drop order: children first (deliveries reference endpoints, grants reference
# api_keys). Only these four tables are this revision's to remove.
NEW_TABLES = (
    "webhook_deliveries",
    "webhook_endpoints",
    "api_key_release_grants",
    "workspace_policies",
)

# Columns only this revision's DDL carries. A table present at downgrade time
# but missing the signature was not made here, so it is left alone.
SIGNATURES: dict[str, frozenset[str]] = {
    "webhook_deliveries": frozenset({"endpoint_id", "event_id", "next_attempt_at"}),
    "webhook_endpoints": frozenset({"secret_encrypted", "cursor_event_id", "creator_scopes"}),
    "api_key_release_grants": frozenset({"key_id", "platform", "daily_cap"}),
    "workspace_policies": frozenset({"agent_release"}),
}


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _workspace_column() -> sa.Column[int]:
    # Same shape as the WorkspaceOwned mixin (0014): NOT NULL, default 1, indexed.
    return sa.Column("workspace_id", sa.Integer(), nullable=False, server_default=sa.text("1"))


def upgrade() -> None:
    # 0001 builds a fresh database from the current models; create only what
    # is missing. Offline (`alembic upgrade --sql`) there is no database to
    # inspect, so the DDL is emitted unconditionally.
    existing = set() if context.is_offline_mode() else _table_names()
    if "workspace_policies" not in existing:
        op.create_table(
            "workspace_policies",
            sa.Column("workspace_id", sa.Integer(), nullable=False),
            sa.Column("agent_release", sa.String(length=20), nullable=False),
            sa.Column("updated_by", sa.String(length=200), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["workspaces.id"], name="fk_workspace_policies_workspace"
            ),
            sa.PrimaryKeyConstraint("workspace_id"),
        )
    if "api_key_release_grants" not in existing:
        op.create_table(
            "api_key_release_grants",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("key_id", sa.Integer(), nullable=False),
            sa.Column("platform", sa.String(length=20), nullable=False),
            sa.Column("daily_cap", sa.Integer(), nullable=False),
            sa.Column("created_by", sa.String(length=200), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            _workspace_column(),
            sa.ForeignKeyConstraint(
                ["key_id"], ["api_keys.id"], name="fk_api_key_release_grants_key"
            ),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["workspaces.id"], name="fk_api_key_release_grants_workspace"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("key_id", "platform", name="uq_api_key_release_grant"),
        )
        op.create_index("ix_api_key_release_grants_key_id", "api_key_release_grants", ["key_id"])
        op.create_index(
            "ix_api_key_release_grants_workspace_id", "api_key_release_grants", ["workspace_id"]
        )
    if "webhook_endpoints" not in existing:
        op.create_table(
            "webhook_endpoints",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("url", sa.Text(), nullable=False),
            sa.Column("secret_encrypted", sa.Text(), nullable=False),
            sa.Column("event_types_json", sa.Text(), nullable=False),
            sa.Column("state", sa.String(length=20), nullable=False),
            sa.Column("description", sa.String(length=200), nullable=True),
            sa.Column("created_by", sa.String(length=200), nullable=True),
            sa.Column("creator_scopes", sa.Text(), nullable=True),
            sa.Column("cursor_event_id", sa.Integer(), nullable=False),
            sa.Column("consecutive_failures", sa.Integer(), nullable=False),
            sa.Column("failing_since", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_error", sa.String(length=300), nullable=True),
            sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("disabled_reason", sa.String(length=300), nullable=True),
            _workspace_column(),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["workspaces.id"], name="fk_webhook_endpoints_workspace"
            ),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_webhook_endpoints_workspace_id", "webhook_endpoints", ["workspace_id"])
    if "webhook_deliveries" not in existing:
        op.create_table(
            "webhook_deliveries",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("endpoint_id", sa.Integer(), nullable=False),
            sa.Column("event_id", sa.Integer(), nullable=False),
            sa.Column("event_type", sa.String(length=80), nullable=False),
            sa.Column("state", sa.String(length=20), nullable=False),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_status_code", sa.Integer(), nullable=True),
            sa.Column("last_error", sa.String(length=300), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
            _workspace_column(),
            sa.ForeignKeyConstraint(
                ["endpoint_id"], ["webhook_endpoints.id"], name="fk_webhook_deliveries_endpoint"
            ),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["workspaces.id"], name="fk_webhook_deliveries_workspace"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("endpoint_id", "event_id", name="uq_webhook_delivery_event"),
        )
        op.create_index(
            "ix_webhook_deliveries_state_next",
            "webhook_deliveries",
            ["state", "next_attempt_at"],
        )
        op.create_index("ix_webhook_deliveries_endpoint_id", "webhook_deliveries", ["endpoint_id"])
        op.create_index(
            "ix_webhook_deliveries_workspace_id", "webhook_deliveries", ["workspace_id"]
        )


def downgrade() -> None:
    # Offline there is nothing to inspect: emit the drops in drop order.
    if context.is_offline_mode():
        for table in NEW_TABLES:
            op.drop_table(table)
        return
    existing = _table_names()
    inspector = sa.inspect(op.get_bind())
    # Children first. A table that exists but lacks this revision's signature
    # columns was not created here (it predates the upgrade or belongs to
    # another revision); it is left in place.
    for table in NEW_TABLES:
        if table not in existing:
            continue
        columns = {column["name"] for column in inspector.get_columns(table)}
        if SIGNATURES[table] <= columns:
            op.drop_table(table)
