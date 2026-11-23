"""Add the webhook inbox: raw verified deliveries and push subscriptions.

Revision ID: 0018_webhook_inbox
Revises: 0016_shared_contracts
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

revision = "0018_webhook_inbox"
down_revision = "0016_shared_contracts"
branch_labels = None
depends_on = None


def _table_names() -> set[str] | None:
    """Existing tables, or None when generating SQL offline (nothing can be read)."""
    if context.is_offline_mode():
        return None
    return set(sa.inspect(op.get_bind()).get_table_names())


def _missing(table: str, existing: set[str] | None) -> bool:
    return existing is None or table not in existing


def _ensure_indexes(
    table: str, existing: set[str] | None, indexes: tuple[tuple[str, list[str]], ...]
) -> None:
    # Checked one by one, so a table created by an earlier partial run still
    # gets every index.
    present: set[str] = set()
    if existing is not None and table in existing:
        present = {str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(table)}
    for name, columns in indexes:
        if name not in present:
            op.create_index(name, table, columns)


def upgrade() -> None:
    # 0001 builds a fresh database from the current models; create only what is missing.
    existing = _table_names()
    if _missing("platform_subscriptions", existing):
        op.create_table(
            "platform_subscriptions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("workspace_id", sa.Integer(), server_default=sa.text("1"), nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(length=20), nullable=False),
            sa.Column("topic", sa.String(length=500), nullable=False),
            sa.Column("hub_secret", sa.String(length=128), nullable=True),
            sa.Column("lease_seconds", sa.Integer(), nullable=True),
            sa.Column("subscribed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "state",
                sa.String(length=20),
                server_default=sa.text("'pending'"),
                nullable=False,
            ),
            sa.Column("last_error", sa.String(length=500), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["account_id"],
                ["connected_accounts.id"],
                name="fk_platform_subscriptions_account",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["workspaces.id"], name="fk_platform_subscriptions_workspace"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("account_id", "provider", "topic", name="uq_platform_subscription"),
        )
    _ensure_indexes(
        "platform_subscriptions",
        existing,
        (
            ("ix_platform_subscriptions_workspace_id", ["workspace_id"]),
            ("ix_platform_subscriptions_account_id", ["account_id"]),
        ),
    )
    if _missing("webhook_events", existing):
        op.create_table(
            "webhook_events",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(length=20), nullable=False),
            sa.Column("subscription_id", sa.Integer(), nullable=True),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("signature_ok", sa.Boolean(), nullable=False),
            sa.Column("dedupe_key", sa.String(length=120), nullable=True),
            sa.Column("body", sa.LargeBinary(), nullable=False),
            sa.Column("content_type", sa.String(length=100), nullable=True),
            sa.Column("source", sa.String(length=64), nullable=True),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error", sa.String(length=500), nullable=True),
            sa.Column("account_id", sa.Integer(), nullable=True),
            sa.Column("workspace_id", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(
                ["subscription_id"],
                ["platform_subscriptions.id"],
                name="fk_webhook_events_subscription",
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["account_id"],
                ["connected_accounts.id"],
                name="fk_webhook_events_account",
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["workspaces.id"], name="fk_webhook_events_workspace"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("dedupe_key", name="uq_webhook_events_dedupe_key"),
        )
    _ensure_indexes(
        "webhook_events",
        existing,
        (
            ("ix_webhook_events_provider_received", ["provider", "received_at"]),
            ("ix_webhook_events_processed_at", ["processed_at"]),
        ),
    )


def downgrade() -> None:
    existing = _table_names()
    for table in ("webhook_events", "platform_subscriptions"):
        if not _missing(table, existing):
            op.drop_table(table)
