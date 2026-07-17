"""Add the uncapped editorial scheduling slot.

Revision ID: 0005_editorial_conveyor
Revises: 0004_feedback_and_publisher
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005_editorial_conveyor"
down_revision = "0004_feedback_and_publisher"
branch_labels = None
depends_on = None


def _column_names(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    if "scheduled_publish_at" not in _column_names("proposals"):
        op.add_column(
            "proposals",
            sa.Column("scheduled_publish_at", sa.String(length=40), nullable=True),
        )

    existing_indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("proposals")}
    if "ix_proposals_scheduled_publish_at" not in existing_indexes:
        op.create_index(
            "ix_proposals_scheduled_publish_at",
            "proposals",
            ["scheduled_publish_at"],
        )

    op.execute(
        sa.text(
            """
            UPDATE proposals
            SET scheduled_publish_at = planned_publish_at
            WHERE scheduled_publish_at IS NULL
              AND status IN (
                'approved',
                'internally_scheduled',
                'publishing',
                'externally_scheduled',
                'publish_unverified',
                'published',
                'publish_failed'
              )
            """
        )
    )


def downgrade() -> None:
    if "scheduled_publish_at" in _column_names("proposals"):
        existing_indexes = {
            item["name"] for item in sa.inspect(op.get_bind()).get_indexes("proposals")
        }
        if "ix_proposals_scheduled_publish_at" in existing_indexes:
            op.drop_index(
                "ix_proposals_scheduled_publish_at",
                table_name="proposals",
            )
        op.drop_column("proposals", "scheduled_publish_at")
