"""Add ownership leases for intelligence agent run recovery.

Revision ID: 0013_agent_run_leases
Revises: 0012_creator_platforms
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013_agent_run_leases"
down_revision = "0012_creator_platforms"
branch_labels = None
depends_on = None


LEASE_COLUMNS = (
    sa.Column("lease_owner", sa.String(length=64), nullable=True),
    sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
)


def _columns() -> set[str]:
    return {
        str(column["name"])
        for column in sa.inspect(op.get_bind()).get_columns("intelligence_agent_runs")
    }


def upgrade() -> None:
    existing = _columns()
    with op.batch_alter_table("intelligence_agent_runs") as batch:
        for column in LEASE_COLUMNS:
            if column.name not in existing:
                batch.add_column(column)


def downgrade() -> None:
    existing = _columns()
    with op.batch_alter_table("intelligence_agent_runs") as batch:
        for column in reversed(LEASE_COLUMNS):
            if column.name in existing:
                batch.drop_column(str(column.name))
