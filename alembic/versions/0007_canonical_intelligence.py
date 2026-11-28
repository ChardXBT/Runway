"""Add channel-scoped canonical intelligence evidence.

Revision ID: 0007_canonical_intelligence
Revises: 0006_uncapped_lineup
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op
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


def _offline() -> bool:
    """`alembic upgrade --sql`: nothing can be inspected, so emit every statement."""
    return context.is_offline_mode()


def _recreate() -> str:
    """SQLite rebuilds the table (keeps the declared foreign keys); Postgres alters in place."""
    return "always" if op.get_context().dialect.name == "sqlite" else "auto"


def _table_names() -> set[str]:
    if _offline():
        return set()
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table: str) -> set[str]:
    if _offline():
        return set()
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns(table)}


def _index_names(table: str) -> set[str]:
    if _offline():
        return set()
    return {item["name"] for item in sa.inspect(op.get_bind()).get_indexes(table)}


def _has_sqlite_foreign_key(
    table: str,
    column: str,
    referred_table: str,
) -> bool:
    if _offline():
        return False
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        rows = bind.exec_driver_sql(f'PRAGMA foreign_key_list("{table}")').mappings()
        return any(row["from"] == column and row["table"] == referred_table for row in rows)
    return any(
        referred_table == foreign_key["referred_table"]
        and column in foreign_key["constrained_columns"]
        for foreign_key in sa.inspect(bind).get_foreign_keys(table)
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
        # SQLite cannot ADD CONSTRAINT; Postgres takes plain ALTERs.
        with op.batch_alter_table("proposals", recreate=_recreate()) as batch:
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
    if "ix_proposals_caption_slate_id" not in _index_names("proposals"):
        op.create_index(
            "ix_proposals_caption_slate_id",
            "proposals",
            ["caption_slate_id"],
        )


def downgrade() -> None:
    if _offline() or "caption_slate_id" in _column_names("proposals"):
        if _offline() or "ix_proposals_caption_slate_id" in _index_names("proposals"):
            op.drop_index("ix_proposals_caption_slate_id", table_name="proposals")
        with op.batch_alter_table("proposals", recreate=_recreate()) as batch:
            batch.drop_column("caption_slate_id")

    existing = _table_names()
    for table_name in reversed(NEW_TABLES):
        if _offline() or table_name in existing:
            op.drop_table(table_name)
