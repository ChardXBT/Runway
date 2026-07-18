"""Mark the legacy planning-horizon field as uncapped.

Revision ID: 0006_uncapped_lineup
Revises: 0005_editorial_conveyor
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0006_uncapped_lineup"
down_revision = "0005_editorial_conveyor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("UPDATE channels SET planning_horizon_days = 0"))


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE channels
            SET planning_horizon_days = 10
            WHERE planning_horizon_days = 0
            """
        )
    )
