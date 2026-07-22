"""Add neural-intelligence provenance, arena, reranking, and exposure state.

Revision ID: 0010_neural_intelligence
Revises: 0009_editorial_diversity
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0010_neural_intelligence"
down_revision = "0009_editorial_diversity"
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


def _foreign_keys(table: str) -> set[str]:
    return {
        str(foreign_key["name"])
        for foreign_key in _inspector().get_foreign_keys(table)
        if foreign_key.get("name")
    }


def _add_columns(table: str, columns: list[sa.Column[object]]) -> None:
    existing = _columns(table)
    added = [column for column in columns if column.name not in existing]
    with op.batch_alter_table(table) as batch:
        for column in added:
            batch.add_column(column)
    if added:
        # Defaults are only a backfill aid for populated SQLite tables. The
        # canonical ORM contract has application defaults, not database defaults.
        with op.batch_alter_table(table) as batch:
            for column in added:
                batch.alter_column(
                    str(column.name),
                    existing_type=column.type,
                    server_default=None,
                )


def upgrade() -> None:
    _add_columns(
        "candidate_images",
        [
            sa.Column(
                "topic_eligibility_class",
                sa.String(length=40),
                nullable=False,
                server_default="unassessed",
            ),
            sa.Column("topic_eligibility_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column(
                "representation_provenance_json",
                sa.Text(),
                nullable=False,
                server_default="{}",
            ),
            sa.Column("exploration_metadata_json", sa.Text(), nullable=False, server_default="{}"),
        ],
    )
    _add_columns(
        "generation_runs",
        [
            sa.Column(
                "selection_diagnostics_json",
                sa.Text(),
                nullable=False,
                server_default="{}",
            )
        ],
    )
    if "ix_candidate_images_topic_eligibility_class" not in _indexes("candidate_images"):
        op.create_index(
            "ix_candidate_images_topic_eligibility_class",
            "candidate_images",
            ["topic_eligibility_class"],
        )

    evidence_columns = [
        sa.Column(
            "retrieval_selected_evidence_json", sa.Text(), nullable=False, server_default="[]"
        ),
        sa.Column("model_supplied_evidence_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("model_cited_evidence_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("ranker_used_evidence_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("reranker_run_json", sa.Text(), nullable=False, server_default="{}"),
    ]
    _add_columns("caption_slates", evidence_columns)
    _add_columns(
        "caption_candidate_records",
        [
            sa.Column(
                "model_supplied_evidence_json", sa.Text(), nullable=False, server_default="[]"
            ),
            sa.Column("model_cited_evidence_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("ranker_used_evidence_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("claim_verification_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("reranker_result_json", sa.Text(), nullable=False, server_default="{}"),
        ],
    )

    _add_columns(
        "blind_studies",
        [sa.Column("arm_identities_json", sa.Text(), nullable=False, server_default="{}")],
    )
    _add_columns(
        "blind_study_cases",
        [
            sa.Column("third_caption", sa.Text()),
            sa.Column("third_candidate_id", sa.Integer()),
            sa.Column("arm_metrics_json", sa.Text(), nullable=False, server_default="{}"),
        ],
    )
    if "ix_blind_study_cases_third_candidate_id" not in _indexes("blind_study_cases"):
        with op.batch_alter_table("blind_study_cases") as batch:
            batch.create_foreign_key(
                "fk_blind_study_cases_third_candidate",
                "caption_candidate_records",
                ["third_candidate_id"],
                ["id"],
            )
        op.create_index(
            "ix_blind_study_cases_third_candidate_id",
            "blind_study_cases",
            ["third_candidate_id"],
        )
    _add_columns(
        "blind_study_responses",
        [
            sa.Column("grounding_problems_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("genericness_score", sa.Float()),
            sa.Column("repetition_score", sa.Float()),
            sa.Column("model_cost_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("latency_json", sa.Text(), nullable=False, server_default="{}"),
        ],
    )

    if "candidate_exposures" not in _tables():
        op.create_table(
            "candidate_exposures",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            sa.Column("candidate_image_id", sa.Integer(), nullable=False),
            sa.Column("search_run_id", sa.Integer()),
            sa.Column("caption_slate_id", sa.Integer()),
            sa.Column("session_key", sa.String(length=128), nullable=False),
            sa.Column("event_type", sa.String(length=40), nullable=False),
            sa.Column("cluster_key", sa.String(length=128)),
            sa.Column("pre_display_score", sa.Float(), nullable=False),
            sa.Column("final_display_probability", sa.Float(), nullable=False),
            sa.Column("display_position", sa.Integer()),
            sa.Column(
                "exploration_policy",
                sa.String(length=80),
                nullable=False,
            ),
            sa.Column("randomized", sa.Boolean(), nullable=False),
            sa.Column("eligible_pool_size", sa.Integer(), nullable=False),
            sa.Column("selected_reason", sa.Text()),
            sa.Column("withheld_reason", sa.Text()),
            sa.Column("representation_sets_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
            sa.ForeignKeyConstraint(["candidate_image_id"], ["candidate_images.id"]),
            sa.ForeignKeyConstraint(["search_run_id"], ["search_runs.id"]),
            sa.ForeignKeyConstraint(["caption_slate_id"], ["caption_slates.id"]),
            sa.UniqueConstraint(
                "candidate_image_id",
                "session_key",
                "event_type",
                name="uq_candidate_exposure_event",
            ),
        )
        for column in (
            "channel_id",
            "candidate_image_id",
            "search_run_id",
            "caption_slate_id",
            "session_key",
            "event_type",
            "cluster_key",
        ):
            op.create_index(f"ix_candidate_exposures_{column}", "candidate_exposures", [column])
        op.create_index(
            "ix_candidate_exposure_session",
            "candidate_exposures",
            ["channel_id", "session_key", "created_at"],
        )
        op.create_index(
            "ix_candidate_exposure_cluster",
            "candidate_exposures",
            ["channel_id", "cluster_key", "event_type"],
        )

    if "multimodal_rerank_runs" not in _tables():
        op.create_table(
            "multimodal_rerank_runs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            sa.Column("candidate_image_id", sa.Integer(), nullable=False),
            sa.Column("caption_slate_id", sa.Integer()),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("provider", sa.String(length=100), nullable=False),
            sa.Column("model", sa.String(length=200), nullable=False),
            sa.Column("prompt_version", sa.String(length=80), nullable=False),
            sa.Column("label_source", sa.String(length=40), nullable=False),
            sa.Column("input_json", sa.Text(), nullable=False),
            sa.Column("component_scores_json", sa.Text(), nullable=False),
            sa.Column("baseline_order_json", sa.Text(), nullable=False),
            sa.Column("final_order_json", sa.Text(), nullable=False),
            sa.Column("changed_order", sa.Boolean(), nullable=False),
            sa.Column("configuration_json", sa.Text(), nullable=False),
            sa.Column("configuration_hash", sa.String(length=64), nullable=False),
            sa.Column("representation_sets_json", sa.Text(), nullable=False),
            sa.Column("latency_ms", sa.Float()),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
            sa.ForeignKeyConstraint(["candidate_image_id"], ["candidate_images.id"]),
            sa.ForeignKeyConstraint(["caption_slate_id"], ["caption_slates.id"]),
        )
        for column in (
            "channel_id",
            "candidate_image_id",
            "caption_slate_id",
            "status",
            "label_source",
            "configuration_hash",
        ):
            op.create_index(
                f"ix_multimodal_rerank_runs_{column}", "multimodal_rerank_runs", [column]
            )

    if "composed_retrieval_examples" not in _tables():
        op.create_table(
            "composed_retrieval_examples",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            sa.Column("reference_media_asset_id", sa.Integer(), nullable=False),
            sa.Column("modification_instruction", sa.Text(), nullable=False),
            sa.Column("target_media_asset_id", sa.Integer(), nullable=False),
            sa.Column("label_source", sa.String(length=40), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("split", sa.String(length=40), nullable=False),
            sa.Column("reviewed", sa.Boolean(), nullable=False),
            sa.Column("instruction_source_json", sa.Text(), nullable=False),
            sa.Column("representation_sets_json", sa.Text(), nullable=False),
            sa.Column("evaluation_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
            sa.ForeignKeyConstraint(["reference_media_asset_id"], ["media_assets.id"]),
            sa.ForeignKeyConstraint(["target_media_asset_id"], ["media_assets.id"]),
            sa.UniqueConstraint(
                "channel_id",
                "reference_media_asset_id",
                "modification_instruction",
                "target_media_asset_id",
                name="uq_composed_retrieval_example",
            ),
        )
        for column in (
            "channel_id",
            "reference_media_asset_id",
            "target_media_asset_id",
            "label_source",
            "status",
            "split",
            "reviewed",
        ):
            op.create_index(
                f"ix_composed_retrieval_examples_{column}",
                "composed_retrieval_examples",
                [column],
            )


def downgrade() -> None:
    for table in (
        "composed_retrieval_examples",
        "multimodal_rerank_runs",
        "candidate_exposures",
    ):
        if table in _tables():
            op.drop_table(table)

    response_columns = _columns("blind_study_responses")
    with op.batch_alter_table("blind_study_responses") as batch:
        for name in (
            "latency_json",
            "model_cost_json",
            "repetition_score",
            "genericness_score",
            "grounding_problems_json",
        ):
            if name in response_columns:
                batch.drop_column(name)

    if "ix_blind_study_cases_third_candidate_id" in _indexes("blind_study_cases"):
        op.drop_index(
            "ix_blind_study_cases_third_candidate_id",
            table_name="blind_study_cases",
        )
    case_foreign_keys = _foreign_keys("blind_study_cases")
    case_columns = _columns("blind_study_cases")
    with op.batch_alter_table("blind_study_cases") as batch:
        if "fk_blind_study_cases_third_candidate" in case_foreign_keys:
            batch.drop_constraint(
                "fk_blind_study_cases_third_candidate",
                type_="foreignkey",
            )
        for name in ("arm_metrics_json", "third_candidate_id", "third_caption"):
            if name in case_columns:
                batch.drop_column(name)
    if "arm_identities_json" in _columns("blind_studies"):
        with op.batch_alter_table("blind_studies") as batch:
            batch.drop_column("arm_identities_json")

    candidate_record_columns = _columns("caption_candidate_records")
    with op.batch_alter_table("caption_candidate_records") as batch:
        for name in (
            "reranker_result_json",
            "claim_verification_json",
            "ranker_used_evidence_json",
            "model_cited_evidence_json",
            "model_supplied_evidence_json",
        ):
            if name in candidate_record_columns:
                batch.drop_column(name)

    slate_columns = _columns("caption_slates")
    with op.batch_alter_table("caption_slates") as batch:
        for name in (
            "reranker_run_json",
            "ranker_used_evidence_json",
            "model_cited_evidence_json",
            "model_supplied_evidence_json",
            "retrieval_selected_evidence_json",
        ):
            if name in slate_columns:
                batch.drop_column(name)

    if "ix_candidate_images_topic_eligibility_class" in _indexes("candidate_images"):
        op.drop_index(
            "ix_candidate_images_topic_eligibility_class",
            table_name="candidate_images",
        )
    candidate_columns = _columns("candidate_images")
    with op.batch_alter_table("candidate_images") as batch:
        for name in (
            "exploration_metadata_json",
            "representation_provenance_json",
            "topic_eligibility_json",
            "topic_eligibility_class",
        ):
            if name in candidate_columns:
                batch.drop_column(name)
    if "selection_diagnostics_json" in _columns("generation_runs"):
        with op.batch_alter_table("generation_runs") as batch:
            batch.drop_column("selection_diagnostics_json")
