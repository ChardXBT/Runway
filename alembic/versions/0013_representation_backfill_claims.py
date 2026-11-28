"""Add leased claim timestamps to representation backfill items.

Revision ID: 0013_representation_backfill_claims
Revises: 0012_creator_platforms
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013_representation_backfill_claims"
down_revision = "0012_creator_platforms"
branch_labels = None
depends_on = None


def _columns(table_name: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def upgrade() -> None:
    if "claimed_at" not in _columns("representation_set_items"):
        op.add_column(
            "representation_set_items",
            sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    if "claimed_at" in _columns("representation_set_items"):
        op.drop_column("representation_set_items", "claimed_at")
