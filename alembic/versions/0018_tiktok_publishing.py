"""Incremental-consent marker on oauth_states (spec 08 section 1).

Revision ID: 0018_tiktok_publishing
Revises: 0017_publishing_engine

Adds `for_account_id` to `oauth_states`: when a state is issued for a
publishing-enable re-consent it records which existing account asked for it,
so the callback can refuse to attach the grant to any other account's
identity. NULL is an ordinary connect.

A fresh database (built by 0001 from the current models) already has the
column, so the add is conditional.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

revision = "0018_tiktok_publishing"
down_revision = "0017_publishing_engine"
branch_labels = None
depends_on = None


def _offline() -> bool:
    return context.is_offline_mode()


def _column_names(table: str) -> set[str]:
    if _offline():
        return set()
    return {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "for_account_id" not in _column_names("oauth_states"):
        op.add_column("oauth_states", sa.Column("for_account_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    if _offline() or "for_account_id" in _column_names("oauth_states"):
        op.drop_column("oauth_states", "for_account_id")
