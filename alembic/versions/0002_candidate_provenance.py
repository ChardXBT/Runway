"""Add candidate-level source provenance.

Revision ID: 0002_candidate_provenance
Revises: 0001_initial
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_candidate_provenance"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _column_names() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns("candidate_images")}


def upgrade() -> None:
    existing = _column_names()
    columns = [
        sa.Column("source_page_url", sa.Text(), nullable=True),
        sa.Column("direct_image_url", sa.Text(), nullable=True),
        sa.Column("source_domain", sa.String(length=255), nullable=True),
        sa.Column("rights_status", sa.String(length=40), nullable=False, server_default="unknown"),
        sa.Column("original_width", sa.Integer(), nullable=True),
        sa.Column("original_height", sa.Integer(), nullable=True),
        sa.Column("provider_result_json", sa.Text(), nullable=False, server_default="{}"),
    ]
    for column in columns:
        if column.name not in existing:
            op.add_column("candidate_images", column)
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("candidate_images")}
    if "ix_candidate_images_direct_image_url" not in indexes:
        op.create_index(
            "ix_candidate_images_direct_image_url",
            "candidate_images",
            ["direct_image_url"],
        )
    if "ix_candidate_images_source_domain" not in indexes:
        op.create_index("ix_candidate_images_source_domain", "candidate_images", ["source_domain"])


def downgrade() -> None:
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("candidate_images")}
    for index in ("ix_candidate_images_direct_image_url", "ix_candidate_images_source_domain"):
        if index in indexes:
            op.drop_index(index, table_name="candidate_images")
    existing = _column_names()
    for name in (
        "provider_result_json",
        "original_height",
        "original_width",
        "rights_status",
        "source_domain",
        "direct_image_url",
        "source_page_url",
    ):
        if name in existing:
            op.drop_column("candidate_images", name)
