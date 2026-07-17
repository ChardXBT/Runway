"""Add caption feedback and guarded external-publisher state.

Revision ID: 0004_feedback_and_publisher
Revises: 0003_proposal_caption_context
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_feedback_and_publisher"
down_revision = "0003_proposal_caption_context"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _column_names(table: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {column["name"] for column in inspector.get_columns(table)}


def upgrade() -> None:
    proposal_columns = _column_names("proposals")
    additions = [
        sa.Column("rights_decision", sa.String(length=40), nullable=True),
        sa.Column("rights_reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("external_post_id", sa.String(length=200), nullable=True),
        sa.Column("external_post_url", sa.Text(), nullable=True),
        sa.Column("scheduled_verified_at", sa.DateTime(timezone=True), nullable=True),
    ]
    for column in additions:
        if column.name not in proposal_columns:
            op.add_column("proposals", column)

    tables = _table_names()
    if "caption_feedback" not in tables:
        op.create_table(
            "caption_feedback",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("proposal_id", sa.Integer(), nullable=False),
            sa.Column("candidate_image_id", sa.Integer(), nullable=False),
            sa.Column("verdict", sa.String(length=40), nullable=False),
            sa.Column("generated_caption", sa.Text(), nullable=False),
            sa.Column("preferred_caption", sa.Text(), nullable=True),
            sa.Column("preferred_structure", sa.String(length=40), nullable=True),
            sa.Column("reason_codes_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("image_verdict", sa.String(length=40), nullable=True),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["candidate_image_id"], ["candidate_images.id"]),
            sa.ForeignKeyConstraint(["proposal_id"], ["proposals.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_caption_feedback_candidate_image_id",
            "caption_feedback",
            ["candidate_image_id"],
        )
        op.create_index("ix_caption_feedback_image_verdict", "caption_feedback", ["image_verdict"])
        op.create_index(
            "ix_caption_feedback_preferred_structure",
            "caption_feedback",
            ["preferred_structure"],
        )
        op.create_index("ix_caption_feedback_proposal_id", "caption_feedback", ["proposal_id"])
        op.create_index("ix_caption_feedback_verdict", "caption_feedback", ["verdict"])

    if "publish_attempts" not in tables:
        op.create_table(
            "publish_attempts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("proposal_id", sa.Integer(), nullable=False),
            sa.Column("publisher", sa.String(length=80), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("confirmation_token_hash", sa.String(length=64), nullable=False),
            sa.Column("payload_hash", sa.String(length=64), nullable=False),
            sa.Column("planned_publish_at", sa.String(length=40), nullable=False),
            sa.Column("screenshot_paths_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("external_id", sa.String(length=200), nullable=True),
            sa.Column("external_url", sa.Text(), nullable=True),
            sa.Column("error_summary", sa.Text(), nullable=True),
            sa.Column("prepared_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["proposal_id"], ["proposals.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("confirmation_token_hash"),
        )
        op.create_index("ix_publish_attempts_proposal_id", "publish_attempts", ["proposal_id"])
        op.create_index("ix_publish_attempts_publisher", "publish_attempts", ["publisher"])
        op.create_index("ix_publish_attempts_status", "publish_attempts", ["status"])

    existing_indexes = {item["name"] for item in sa.inspect(op.get_bind()).get_indexes("proposals")}
    if "ix_proposals_rights_decision" not in existing_indexes:
        op.create_index("ix_proposals_rights_decision", "proposals", ["rights_decision"])
    if "ix_proposals_external_post_id" not in existing_indexes:
        op.create_index("ix_proposals_external_post_id", "proposals", ["external_post_id"])


def downgrade() -> None:
    tables = _table_names()
    if "publish_attempts" in tables:
        op.drop_table("publish_attempts")
    if "caption_feedback" in tables:
        op.drop_table("caption_feedback")

    proposal_columns = _column_names("proposals")
    for name in (
        "scheduled_verified_at",
        "external_post_url",
        "external_post_id",
        "rights_reviewed_at",
        "rights_decision",
    ):
        if name in proposal_columns:
            op.drop_column("proposals", name)
