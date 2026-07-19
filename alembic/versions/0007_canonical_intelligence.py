"""Add channel-scoped canonical intelligence evidence.

Revision ID: 0007_canonical_intelligence
Revises: 0006_uncapped_lineup
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op
from runway.db import models  # noqa: F401
from runway.db.base import Base

revision = "0007_canonical_intelligence"
down_revision = "0006_uncapped_lineup"
branch_labels = None
depends_on = None

NEW_TABLES = (
    "representation_records",
    "channel_policy_rules",
    "intelligence_retrieval_runs",
    "retrieval_evidence_records",
    "caption_slates",
    "caption_candidate_records",
    "caption_exposures",
    "pairwise_preferences",
    "feedback_signals",
    "image_generation_runs",
    "generated_asset_lineage",
    "intelligence_experiments",
)


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _has_sqlite_foreign_key(
    table: str,
    column: str,
    referred_table: str,
) -> bool:
    rows = op.get_bind().exec_driver_sql(
        f'PRAGMA foreign_key_list("{table}")'
    ).mappings()
    return any(
        row["from"] == column and row["table"] == referred_table
        for row in rows
    )


def upgrade() -> None:
    bind = op.get_bind()
    existing = _table_names()
    for table_name in NEW_TABLES:
        if table_name not in existing:
            Base.metadata.tables[table_name].create(bind=bind, checkfirst=True)

    column_exists = "caption_slate_id" in _column_names("proposals")
    foreign_key_name = "fk_proposals_caption_slate_id_caption_slates"
    foreign_key_exists = _has_sqlite_foreign_key(
        "proposals",
        "caption_slate_id",
        "caption_slates",
    )
    if not column_exists or not foreign_key_exists:
        with op.batch_alter_table("proposals", recreate="always") as batch:
            if not column_exists:
                batch.add_column(
                    sa.Column(
                        "caption_slate_id",
                        sa.Integer(),
                        nullable=True,
                    )
                )
            if not foreign_key_exists:
                batch.create_foreign_key(
                    foreign_key_name,
                    "caption_slates",
                    ["caption_slate_id"],
                    ["id"],
                )
    indexes = {item["name"] for item in sa.inspect(bind).get_indexes("proposals")}
    if "ix_proposals_caption_slate_id" not in indexes:
        op.create_index(
            "ix_proposals_caption_slate_id",
            "proposals",
            ["caption_slate_id"],
        )


def downgrade() -> None:
    if "caption_slate_id" in _column_names("proposals"):
        indexes = {
            item["name"] for item in sa.inspect(op.get_bind()).get_indexes("proposals")
        }
        if "ix_proposals_caption_slate_id" in indexes:
            op.drop_index("ix_proposals_caption_slate_id", table_name="proposals")
        with op.batch_alter_table("proposals", recreate="always") as batch:
            batch.drop_column("caption_slate_id")

    existing = _table_names()
    for table_name in reversed(NEW_TABLES):
        if table_name in existing:
            op.drop_table(table_name)
