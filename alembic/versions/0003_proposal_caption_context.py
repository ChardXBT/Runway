"""Persist structured caption-generation context on proposals.

Revision ID: 0003_proposal_caption_context
Revises: 0002_candidate_provenance
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

revision = "0003_proposal_caption_context"
down_revision = "0002_candidate_provenance"
branch_labels = None
depends_on = None


def _offline() -> bool:
    """`alembic upgrade --sql`: nothing can be inspected, so emit every statement."""
    return context.is_offline_mode()


def _column_names() -> set[str]:
    if _offline():
        return set()
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns("proposals")}


def upgrade() -> None:
    existing = _column_names()
    columns = [
        sa.Column(
            "caption_rationale",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column("caption_confidence", sa.Float(), nullable=True),
        sa.Column(
            "caption_reference_post_ids_json",
            sa.Text(),
            nullable=False,
            server_default="[]",
        ),
        sa.Column("factual_uncertainty_warning", sa.Text(), nullable=True),
    ]
    for column in columns:
        if column.name not in existing:
            op.add_column("proposals", column)


def downgrade() -> None:
    existing = _column_names()
    for name in (
        "factual_uncertainty_warning",
        "caption_reference_post_ids_json",
        "caption_confidence",
        "caption_rationale",
    ):
        if _offline() or name in existing:
            op.drop_column("proposals", name)
