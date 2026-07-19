"""Add candidate diversity fingerprints and isolated shadow editorial decisions.

Revision ID: 0009_editorial_diversity
Revises: 0008_intelligence_data_flywheel
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0009_editorial_diversity"
down_revision = "0008_intelligence_data_flywheel"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _tables() -> set[str]:
    return set(_inspector().get_table_names())


def _columns(table: str) -> set[str]:
    return {str(column["name"]) for column in _inspector().get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {str(index["name"]) for index in _inspector().get_indexes(table)}


def upgrade() -> None:
    candidate_columns = _columns("candidate_images")
    added_fingerprint = "diversity_fingerprint_json" not in candidate_columns
    with op.batch_alter_table("candidate_images") as batch:
        if "diversity_cluster_key" not in candidate_columns:
            batch.add_column(sa.Column("diversity_cluster_key", sa.String(length=128)))
        if added_fingerprint:
            batch.add_column(
                sa.Column(
                    "diversity_fingerprint_json",
                    sa.Text(),
                    nullable=True,
                )
            )
    if added_fingerprint:
        op.execute(
            sa.text(
                "UPDATE candidate_images SET diversity_fingerprint_json = '{}' "
                "WHERE diversity_fingerprint_json IS NULL"
            )
        )
        with op.batch_alter_table("candidate_images") as batch:
            batch.alter_column(
                "diversity_fingerprint_json",
                existing_type=sa.Text(),
                nullable=False,
                server_default=None,
            )
    if "ix_candidate_images_diversity_cluster_key" not in _indexes("candidate_images"):
        op.create_index(
            "ix_candidate_images_diversity_cluster_key",
            "candidate_images",
            ["diversity_cluster_key"],
        )

    if "shadow_editorial_decisions" not in _tables():
        op.create_table(
            "shadow_editorial_decisions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            sa.Column("proposal_id", sa.Integer(), nullable=False),
            sa.Column("candidate_image_id", sa.Integer(), nullable=False),
            sa.Column("agent_run_id", sa.Integer()),
            sa.Column("evaluator_version", sa.String(length=80), nullable=False),
            sa.Column("label_source", sa.String(length=40), nullable=False),
            sa.Column("training_eligible", sa.Boolean(), nullable=False),
            sa.Column("decision", sa.String(length=40), nullable=False),
            sa.Column("edited_caption", sa.Text()),
            sa.Column("image_score", sa.Float(), nullable=False),
            sa.Column("caption_score", sa.Float(), nullable=False),
            sa.Column("pairing_score", sa.Float(), nullable=False),
            sa.Column("confidence", sa.Float(), nullable=False),
            sa.Column("reason_codes_json", sa.Text(), nullable=False),
            sa.Column("rationale", sa.Text(), nullable=False),
            sa.Column("diversity_cluster_key", sa.String(length=128)),
            sa.Column("input_snapshot_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["agent_run_id"], ["intelligence_agent_runs.id"]),
            sa.ForeignKeyConstraint(["candidate_image_id"], ["candidate_images.id"]),
            sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
            sa.ForeignKeyConstraint(["proposal_id"], ["proposals.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "proposal_id",
                "evaluator_version",
                name="uq_shadow_editorial_proposal_evaluator",
            ),
        )
        for column in (
            "channel_id",
            "proposal_id",
            "candidate_image_id",
            "agent_run_id",
            "evaluator_version",
            "label_source",
            "training_eligible",
            "decision",
            "diversity_cluster_key",
        ):
            op.create_index(
                f"ix_shadow_editorial_decisions_{column}",
                "shadow_editorial_decisions",
                [column],
            )
        op.create_index(
            "ix_shadow_editorial_lookup",
            "shadow_editorial_decisions",
            ["channel_id", "decision", "created_at"],
        )


def downgrade() -> None:
    if "shadow_editorial_decisions" in _tables():
        op.drop_table("shadow_editorial_decisions")
    if "ix_candidate_images_diversity_cluster_key" in _indexes("candidate_images"):
        op.drop_index(
            "ix_candidate_images_diversity_cluster_key",
            table_name="candidate_images",
        )
    candidate_columns = _columns("candidate_images")
    with op.batch_alter_table("candidate_images") as batch:
        if "diversity_fingerprint_json" in candidate_columns:
            batch.drop_column("diversity_fingerprint_json")
        if "diversity_cluster_key" in candidate_columns:
            batch.drop_column("diversity_cluster_key")
