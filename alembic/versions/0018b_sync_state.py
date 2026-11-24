"""Adaptive sync state, provider quota use, and workspace last-seen.

Revision ID: 0018b_sync_state
Revises: 0018_webhook_inbox
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

revision = "0018b_sync_state"
down_revision = "0018_webhook_inbox"
branch_labels = None
depends_on = None


def _table_names() -> set[str] | None:
    """Existing tables, or None when generating SQL offline (nothing can be read)."""
    if context.is_offline_mode():
        return None
    return set(sa.inspect(op.get_bind()).get_table_names())


def _missing(table: str, existing: set[str] | None) -> bool:
    return existing is None or table not in existing


def _has_column(table: str, column: str, existing: set[str] | None) -> bool:
    if existing is None or table not in existing:
        # Offline (or, defensively, a missing parent): emit the statement.
        return False
    names = {str(item["name"]) for item in sa.inspect(op.get_bind()).get_columns(table)}
    return column in names


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
    if _missing("account_sync_state", existing):
        op.create_table(
            "account_sync_state",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("workspace_id", sa.Integer(), server_default=sa.text("1"), nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("scope", sa.String(length=50), nullable=False),
            sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_status", sa.String(length=20), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("boosted_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("source", sa.String(length=10), nullable=True),
            sa.Column(
                "consecutive_failures",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            ),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["account_id"],
                ["connected_accounts.id"],
                name="fk_account_sync_state_account",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["workspaces.id"], name="fk_account_sync_state_workspace"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("account_id", "scope", name="uq_account_sync_state_scope"),
        )
    _ensure_indexes(
        "account_sync_state",
        existing,
        (
            ("ix_account_sync_state_workspace_id", ["workspace_id"]),
            ("ix_account_sync_state_account_id", ["account_id"]),
            ("ix_account_sync_state_due", ["next_run_at"]),
        ),
    )
    if _missing("quota_usage", existing):
        op.create_table(
            "quota_usage",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("provider", sa.String(length=20), nullable=False),
            sa.Column("bucket", sa.String(length=60), nullable=False),
            sa.Column("day", sa.Date(), nullable=False),
            sa.Column(
                "units_used", sa.Integer(), server_default=sa.text("0"), nullable=False
            ),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("provider", "bucket", "day", name="uq_quota_usage_day"),
        )
    if not _has_column("workspaces", "last_seen_at", existing):
        op.add_column(
            "workspaces", sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    existing = _table_names()
    for table in ("quota_usage", "account_sync_state"):
        if not _missing(table, existing):
            op.drop_table(table)
    if _has_column("workspaces", "last_seen_at", existing):
        op.drop_column("workspaces", "last_seen_at")
