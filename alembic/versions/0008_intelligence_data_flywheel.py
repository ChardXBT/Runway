"""Add the canonical intelligence-data flywheel lifecycle.

Revision ID: 0008_intelligence_data_flywheel
Revises: 0007_canonical_intelligence
"""

from __future__ import annotations

from collections.abc import Iterable

import sqlalchemy as sa

from alembic import op

revision = "0008_intelligence_data_flywheel"
down_revision = "0007_canonical_intelligence"
branch_labels = None
depends_on = None


NEW_TABLES = (
    "representation_sets",
    "representation_set_items",
    "intelligence_activations",
    "intelligence_agent_runs",
    "intelligence_agent_steps",
    "preference_datasets",
    "preference_dataset_items",
    "preference_model_versions",
    "annotation_refresh_runs",
    "annotation_refresh_items",
    "blind_studies",
    "blind_study_cases",
    "blind_study_responses",
    "active_learning_batches",
    "active_learning_selections",
)


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _table_names() -> set[str]:
    return set(_inspector().get_table_names())


def _column_names(table: str) -> set[str]:
    return {str(column["name"]) for column in _inspector().get_columns(table)}


def _index_names(table: str) -> set[str]:
    return {str(index["name"]) for index in _inspector().get_indexes(table)}


def _unique_columns(table: str) -> set[tuple[str, ...]]:
    return {
        tuple(str(value) for value in constraint.get("column_names", []))
        for constraint in _inspector().get_unique_constraints(table)
    }


def _has_foreign_key(table: str, column: str, referred_table: str) -> bool:
    return any(
        tuple(foreign_key.get("constrained_columns", [])) == (column,)
        and foreign_key.get("referred_table") == referred_table
        for foreign_key in _inspector().get_foreign_keys(table)
    )


def _create_representation_tables() -> None:
    existing = _table_names()
    if "representation_sets" not in existing:
        op.create_table(
            "representation_sets",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            sa.Column("scope", sa.String(length=100), nullable=False),
            sa.Column("purpose", sa.String(length=80), nullable=False),
            sa.Column("modality", sa.String(length=40), nullable=False),
            sa.Column("provider", sa.String(length=100), nullable=False),
            sa.Column("model", sa.String(length=200), nullable=False),
            sa.Column("model_version", sa.String(length=100), nullable=False),
            sa.Column("configuration_json", sa.Text(), nullable=False),
            sa.Column("configuration_hash", sa.String(length=64), nullable=False),
            sa.Column("plan_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("expected_count", sa.Integer(), nullable=False),
            sa.Column("completed_count", sa.Integer(), nullable=False),
            sa.Column("failed_count", sa.Integer(), nullable=False),
            sa.Column("stale_count", sa.Integer(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("supersedes_set_id", sa.Integer(), nullable=True),
            sa.Column("error_summary", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
            sa.ForeignKeyConstraint(
                ["supersedes_set_id"],
                ["representation_sets.id"],
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "channel_id",
                "scope",
                "purpose",
                "plan_hash",
                name="uq_representation_set_plan",
            ),
        )
        op.create_index(
            "ix_representation_sets_channel_id",
            "representation_sets",
            ["channel_id"],
        )
        op.create_index(
            "ix_representation_sets_scope",
            "representation_sets",
            ["scope"],
        )
        op.create_index(
            "ix_representation_sets_purpose",
            "representation_sets",
            ["purpose"],
        )
        op.create_index(
            "ix_representation_sets_configuration_hash",
            "representation_sets",
            ["configuration_hash"],
        )
        op.create_index(
            "ix_representation_sets_plan_hash",
            "representation_sets",
            ["plan_hash"],
        )
        op.create_index(
            "ix_representation_sets_status",
            "representation_sets",
            ["status"],
        )
        op.create_index(
            "ix_representation_sets_active",
            "representation_sets",
            ["active"],
        )
        op.create_index(
            "ix_representation_set_resolution",
            "representation_sets",
            ["channel_id", "scope", "purpose", "active", "status"],
        )

    existing = _table_names()
    if "representation_set_items" not in existing:
        op.create_table(
            "representation_set_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("representation_set_id", sa.Integer(), nullable=False),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            sa.Column("entity_type", sa.String(length=80), nullable=False),
            sa.Column("entity_id", sa.Integer(), nullable=False),
            sa.Column("field", sa.String(length=80), nullable=False),
            sa.Column("source_content_hash", sa.String(length=64), nullable=False),
            sa.Column("source_locator_json", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("representation_record_id", sa.Integer(), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("error_summary", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
            sa.ForeignKeyConstraint(
                ["representation_record_id"],
                ["representation_records.id"],
            ),
            sa.ForeignKeyConstraint(
                ["representation_set_id"],
                ["representation_sets.id"],
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "representation_set_id",
                "entity_type",
                "entity_id",
                "field",
                name="uq_representation_set_item",
            ),
        )
        for column in (
            "representation_set_id",
            "channel_id",
            "source_content_hash",
            "status",
            "representation_record_id",
        ):
            op.create_index(
                f"ix_representation_set_items_{column}",
                "representation_set_items",
                [column],
            )
        op.create_index(
            "ix_representation_set_item_checkpoint",
            "representation_set_items",
            ["representation_set_id", "status", "id"],
        )

    if "intelligence_activations" not in _table_names():
        op.create_table(
            "intelligence_activations",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            sa.Column("resource_type", sa.String(length=80), nullable=False),
            sa.Column("target", sa.String(length=100), nullable=False),
            sa.Column("action", sa.String(length=40), nullable=False),
            sa.Column("resource_id", sa.String(length=100), nullable=False),
            sa.Column("previous_resource_id", sa.String(length=100), nullable=True),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("gate_results_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        for column in ("channel_id", "resource_type", "target", "action"):
            op.create_index(
                f"ix_intelligence_activations_{column}",
                "intelligence_activations",
                [column],
            )
        op.create_index(
            "ix_intelligence_activation_lookup",
            "intelligence_activations",
            ["channel_id", "resource_type", "target", "created_at"],
        )


def _create_agent_tables() -> None:
    if "intelligence_agent_runs" not in _table_names():
        op.create_table(
            "intelligence_agent_runs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            sa.Column("run_key", sa.String(length=128), nullable=False),
            sa.Column("capability", sa.String(length=100), nullable=False),
            sa.Column("provider", sa.String(length=100), nullable=False),
            sa.Column("model", sa.String(length=200), nullable=False),
            sa.Column("prompt_version", sa.String(length=80), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("input_json", sa.Text(), nullable=False),
            sa.Column("output_json", sa.Text(), nullable=False),
            sa.Column("budget_json", sa.Text(), nullable=False),
            sa.Column("usage_json", sa.Text(), nullable=False),
            sa.Column("configuration_hash", sa.String(length=64), nullable=False),
            sa.Column("attempt_count", sa.Integer(), nullable=False),
            sa.Column("error_summary", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("run_key", name="uq_intelligence_agent_run_key"),
        )
        for column in ("channel_id", "capability", "status", "configuration_hash"):
            op.create_index(
                f"ix_intelligence_agent_runs_{column}",
                "intelligence_agent_runs",
                [column],
            )
        op.create_index(
            "ix_intelligence_agent_run_lookup",
            "intelligence_agent_runs",
            ["channel_id", "capability", "status", "started_at"],
        )

    if "intelligence_agent_steps" not in _table_names():
        op.create_table(
            "intelligence_agent_steps",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("agent_run_id", sa.Integer(), nullable=False),
            sa.Column("parent_step_id", sa.Integer(), nullable=True),
            sa.Column("sequence", sa.Integer(), nullable=False),
            sa.Column("attempt", sa.Integer(), nullable=False),
            sa.Column("capability", sa.String(length=100), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("input_json", sa.Text(), nullable=False),
            sa.Column("output_json", sa.Text(), nullable=False),
            sa.Column("budget_json", sa.Text(), nullable=False),
            sa.Column("usage_json", sa.Text(), nullable=False),
            sa.Column("timeout_seconds", sa.Integer(), nullable=False),
            sa.Column("artifact_type", sa.String(length=80), nullable=True),
            sa.Column("artifact_id", sa.String(length=100), nullable=True),
            sa.Column("error_summary", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["agent_run_id"], ["intelligence_agent_runs.id"]),
            sa.ForeignKeyConstraint(["parent_step_id"], ["intelligence_agent_steps.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "agent_run_id",
                "sequence",
                "attempt",
                name="uq_intelligence_agent_step_attempt",
            ),
        )
        for column in ("agent_run_id", "capability", "status"):
            op.create_index(
                f"ix_intelligence_agent_steps_{column}",
                "intelligence_agent_steps",
                [column],
            )
        op.create_index(
            "ix_intelligence_agent_step_lookup",
            "intelligence_agent_steps",
            ["agent_run_id", "sequence", "status"],
        )


def _create_preference_tables() -> None:
    if "preference_datasets" not in _table_names():
        op.create_table(
            "preference_datasets",
            sa.Column("dataset_id", sa.String(length=80), nullable=False),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            sa.Column("target", sa.String(length=40), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("feature_schema_version", sa.String(length=80), nullable=False),
            sa.Column("taxonomy_version", sa.String(length=80), nullable=False),
            sa.Column("split_seed", sa.Integer(), nullable=False),
            sa.Column("configuration_json", sa.Text(), nullable=False),
            sa.Column("configuration_hash", sa.String(length=64), nullable=False),
            sa.Column("content_hash", sa.String(length=64), nullable=False),
            sa.Column("row_count", sa.Integer(), nullable=False),
            sa.Column("split_counts_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
            sa.PrimaryKeyConstraint("dataset_id"),
            sa.UniqueConstraint("content_hash", name="uq_preference_dataset_content"),
        )
        for column in (
            "channel_id",
            "target",
            "status",
            "feature_schema_version",
            "configuration_hash",
        ):
            op.create_index(
                f"ix_preference_datasets_{column}",
                "preference_datasets",
                [column],
            )
        op.create_index(
            "ix_preference_dataset_lookup",
            "preference_datasets",
            ["channel_id", "target", "status", "created_at"],
        )

    if "preference_dataset_items" not in _table_names():
        op.create_table(
            "preference_dataset_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("dataset_id", sa.String(length=80), nullable=False),
            sa.Column("pairwise_preference_id", sa.Integer(), nullable=False),
            sa.Column("split", sa.String(length=40), nullable=False),
            sa.Column("group_key", sa.String(length=128), nullable=False),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("preferred_features_json", sa.Text(), nullable=False),
            sa.Column("dispreferred_features_json", sa.Text(), nullable=False),
            sa.Column("strength", sa.Float(), nullable=False),
            sa.Column("snapshot_hash", sa.String(length=64), nullable=False),
            sa.ForeignKeyConstraint(["dataset_id"], ["preference_datasets.dataset_id"]),
            sa.ForeignKeyConstraint(
                ["pairwise_preference_id"],
                ["pairwise_preferences.id"],
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "dataset_id",
                "pairwise_preference_id",
                name="uq_preference_dataset_pair",
            ),
        )
        for column in (
            "dataset_id",
            "pairwise_preference_id",
            "split",
            "group_key",
            "snapshot_hash",
        ):
            op.create_index(
                f"ix_preference_dataset_items_{column}",
                "preference_dataset_items",
                [column],
            )
        op.create_index(
            "ix_preference_dataset_split",
            "preference_dataset_items",
            ["dataset_id", "split", "position"],
        )

    if "preference_model_versions" not in _table_names():
        op.create_table(
            "preference_model_versions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            sa.Column("target", sa.String(length=40), nullable=False),
            sa.Column("algorithm", sa.String(length=80), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("dataset_id", sa.String(length=80), nullable=False),
            sa.Column("parent_model_id", sa.Integer(), nullable=True),
            sa.Column("feature_schema_version", sa.String(length=80), nullable=False),
            sa.Column("feature_names_json", sa.Text(), nullable=False),
            sa.Column("parameters_json", sa.Text(), nullable=False),
            sa.Column("training_configuration_json", sa.Text(), nullable=False),
            sa.Column("metrics_json", sa.Text(), nullable=False),
            sa.Column("calibration_json", sa.Text(), nullable=False),
            sa.Column("label_count", sa.Integer(), nullable=False),
            sa.Column("minimum_label_count", sa.Integer(), nullable=False),
            sa.Column("configuration_hash", sa.String(length=64), nullable=False),
            sa.Column("artifact_hash", sa.String(length=64), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("error_summary", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
            sa.ForeignKeyConstraint(["dataset_id"], ["preference_datasets.dataset_id"]),
            sa.ForeignKeyConstraint(
                ["parent_model_id"],
                ["preference_model_versions.id"],
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "channel_id",
                "target",
                "artifact_hash",
                name="uq_preference_model_artifact",
            ),
        )
        for column in (
            "channel_id",
            "target",
            "status",
            "dataset_id",
            "feature_schema_version",
            "configuration_hash",
            "active",
        ):
            op.create_index(
                f"ix_preference_model_versions_{column}",
                "preference_model_versions",
                [column],
            )
        op.create_index(
            "ix_preference_model_resolution",
            "preference_model_versions",
            ["channel_id", "target", "active", "status"],
        )


def _create_annotation_tables() -> None:
    if "annotation_refresh_runs" not in _table_names():
        op.create_table(
            "annotation_refresh_runs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            sa.Column("annotation_version", sa.String(length=80), nullable=False),
            sa.Column("prompt_version", sa.String(length=80), nullable=False),
            sa.Column("provider", sa.String(length=100), nullable=False),
            sa.Column("model", sa.String(length=200), nullable=False),
            sa.Column("configuration_json", sa.Text(), nullable=False),
            sa.Column("plan_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("expected_count", sa.Integer(), nullable=False),
            sa.Column("completed_count", sa.Integer(), nullable=False),
            sa.Column("failed_count", sa.Integer(), nullable=False),
            sa.Column("active", sa.Boolean(), nullable=False),
            sa.Column("error_summary", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("validated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("activated_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "channel_id",
                "annotation_version",
                "plan_hash",
                name="uq_annotation_refresh_plan",
            ),
        )
        for column in (
            "channel_id",
            "annotation_version",
            "plan_hash",
            "status",
            "active",
        ):
            op.create_index(
                f"ix_annotation_refresh_runs_{column}",
                "annotation_refresh_runs",
                [column],
            )

    if "annotation_refresh_items" not in _table_names():
        op.create_table(
            "annotation_refresh_items",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("annotation_refresh_run_id", sa.Integer(), nullable=False),
            sa.Column("post_id", sa.Integer(), nullable=False),
            sa.Column("source_content_hash", sa.String(length=64), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("post_annotation_id", sa.Integer(), nullable=True),
            sa.Column("attempts", sa.Integer(), nullable=False),
            sa.Column("error_summary", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(
                ["annotation_refresh_run_id"],
                ["annotation_refresh_runs.id"],
            ),
            sa.ForeignKeyConstraint(["post_annotation_id"], ["post_annotations.id"]),
            sa.ForeignKeyConstraint(["post_id"], ["posts.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "annotation_refresh_run_id",
                "post_id",
                name="uq_annotation_refresh_item",
            ),
        )
        for column in (
            "annotation_refresh_run_id",
            "post_id",
            "source_content_hash",
            "status",
        ):
            op.create_index(
                f"ix_annotation_refresh_items_{column}",
                "annotation_refresh_items",
                [column],
            )
        op.create_index(
            "ix_annotation_refresh_checkpoint",
            "annotation_refresh_items",
            ["annotation_refresh_run_id", "status", "id"],
        )


def _create_study_tables() -> None:
    if "blind_studies" not in _table_names():
        op.create_table(
            "blind_studies",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            sa.Column("study_key", sa.String(length=128), nullable=False),
            sa.Column("target", sa.String(length=40), nullable=False),
            sa.Column("baseline_identity", sa.String(length=200), nullable=False),
            sa.Column("challenger_identity", sa.String(length=200), nullable=False),
            sa.Column("seed", sa.Integer(), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("split_policy_json", sa.Text(), nullable=False),
            sa.Column("preregistration_json", sa.Text(), nullable=False),
            sa.Column("case_count", sa.Integer(), nullable=False),
            sa.Column("response_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("study_key", name="uq_blind_study_key"),
        )
        for column in ("channel_id", "target", "status"):
            op.create_index(
                f"ix_blind_studies_{column}",
                "blind_studies",
                [column],
            )

    if "blind_study_cases" not in _table_names():
        op.create_table(
            "blind_study_cases",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("blind_study_id", sa.Integer(), nullable=False),
            sa.Column("case_key", sa.String(length=128), nullable=False),
            sa.Column("candidate_image_id", sa.Integer(), nullable=True),
            sa.Column("media_asset_id", sa.Integer(), nullable=False),
            sa.Column("split", sa.String(length=40), nullable=False),
            sa.Column("group_key", sa.String(length=128), nullable=False),
            sa.Column("first_caption", sa.Text(), nullable=False),
            sa.Column("second_caption", sa.Text(), nullable=False),
            sa.Column("first_candidate_id", sa.Integer(), nullable=True),
            sa.Column("second_candidate_id", sa.Integer(), nullable=True),
            sa.Column("order_token", sa.String(length=40), nullable=False),
            sa.Column("hidden_label_json", sa.Text(), nullable=False),
            sa.Column("metadata_json", sa.Text(), nullable=False),
            sa.Column("selection_rationale_json", sa.Text(), nullable=False),
            sa.Column("display_order", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["blind_study_id"], ["blind_studies.id"]),
            sa.ForeignKeyConstraint(["candidate_image_id"], ["candidate_images.id"]),
            sa.ForeignKeyConstraint(
                ["first_candidate_id"],
                ["caption_candidate_records.id"],
            ),
            sa.ForeignKeyConstraint(["media_asset_id"], ["media_assets.id"]),
            sa.ForeignKeyConstraint(
                ["second_candidate_id"],
                ["caption_candidate_records.id"],
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "blind_study_id",
                "case_key",
                name="uq_blind_study_case",
            ),
        )
        for column in (
            "blind_study_id",
            "candidate_image_id",
            "media_asset_id",
            "first_candidate_id",
            "second_candidate_id",
            "split",
            "group_key",
        ):
            op.create_index(
                f"ix_blind_study_cases_{column}",
                "blind_study_cases",
                [column],
            )
        op.create_index(
            "ix_blind_study_case_order",
            "blind_study_cases",
            ["blind_study_id", "display_order"],
        )

    if "blind_study_responses" not in _table_names():
        op.create_table(
            "blind_study_responses",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("blind_study_case_id", sa.Integer(), nullable=False),
            sa.Column("choice", sa.String(length=40), nullable=False),
            sa.Column("reviewer_kind", sa.String(length=40), nullable=False),
            sa.Column("reviewer_label", sa.String(length=80), nullable=False),
            sa.Column("review_session", sa.String(length=128), nullable=False),
            sa.Column("acceptable_choices_json", sa.Text(), nullable=False),
            sa.Column("edited_final_caption", sa.Text(), nullable=True),
            sa.Column("image_verdict", sa.String(length=40), nullable=True),
            sa.Column("caption_verdict", sa.String(length=40), nullable=True),
            sa.Column("pairing_verdict", sa.String(length=40), nullable=True),
            sa.Column("reason_codes_json", sa.Text(), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("decision_time_ms", sa.Float(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["blind_study_case_id"],
                ["blind_study_cases.id"],
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "blind_study_case_id",
                name="uq_blind_study_response",
            ),
        )
        op.create_index(
            "ix_blind_study_responses_blind_study_case_id",
            "blind_study_responses",
            ["blind_study_case_id"],
        )
        op.create_index(
            "ix_blind_study_responses_review_session",
            "blind_study_responses",
            ["review_session"],
        )

    if "active_learning_batches" not in _table_names():
        op.create_table(
            "active_learning_batches",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            sa.Column("batch_key", sa.String(length=128), nullable=False),
            sa.Column("target", sa.String(length=40), nullable=False),
            sa.Column("strategy_version", sa.String(length=80), nullable=False),
            sa.Column("seed", sa.Integer(), nullable=False),
            sa.Column("configuration_json", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=40), nullable=False),
            sa.Column("selection_count", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "batch_key",
                name="uq_active_learning_batch_key",
            ),
        )
        for column in ("channel_id", "target", "status"):
            op.create_index(
                f"ix_active_learning_batches_{column}",
                "active_learning_batches",
                [column],
            )

    if "active_learning_selections" not in _table_names():
        op.create_table(
            "active_learning_selections",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("active_learning_batch_id", sa.Integer(), nullable=False),
            sa.Column("entity_type", sa.String(length=80), nullable=False),
            sa.Column("entity_id", sa.Integer(), nullable=False),
            sa.Column("group_key", sa.String(length=128), nullable=False),
            sa.Column("split", sa.String(length=40), nullable=False),
            sa.Column("priority_rank", sa.Integer(), nullable=False),
            sa.Column("uncertainty_score", sa.Float(), nullable=False),
            sa.Column("diversity_score", sa.Float(), nullable=False),
            sa.Column("rationale_json", sa.Text(), nullable=False),
            sa.Column("label_status", sa.String(length=40), nullable=False),
            sa.ForeignKeyConstraint(
                ["active_learning_batch_id"],
                ["active_learning_batches.id"],
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "active_learning_batch_id",
                "entity_type",
                "entity_id",
                name="uq_active_learning_selection",
            ),
        )
        for column in (
            "active_learning_batch_id",
            "group_key",
            "split",
            "label_status",
        ):
            op.create_index(
                f"ix_active_learning_selections_{column}",
                "active_learning_selections",
                [column],
            )
        op.create_index(
            "ix_active_learning_priority",
            "active_learning_selections",
            ["active_learning_batch_id", "priority_rank"],
        )


def _add_nullable_columns(
    table: str,
    columns: Iterable[sa.Column[object]],
) -> set[str]:
    existing = _column_names(table)
    added: set[str] = set()
    for column in columns:
        if column.name not in existing:
            column.nullable = True
            op.add_column(table, column)
            added.add(str(column.name))
    return added


def _upgrade_retrieval_runs() -> None:
    added = _add_nullable_columns(
        "intelligence_retrieval_runs",
        (
            sa.Column("representation_sets_json", sa.Text()),
            sa.Column("cache_diagnostics_json", sa.Text()),
        ),
    )
    if added:
        op.execute(
            sa.text(
                "UPDATE intelligence_retrieval_runs "
                "SET representation_sets_json = '{}' "
                "WHERE representation_sets_json IS NULL"
            )
        )
        op.execute(
            sa.text(
                "UPDATE intelligence_retrieval_runs "
                "SET cache_diagnostics_json = '{}' "
                "WHERE cache_diagnostics_json IS NULL"
            )
        )
        with op.batch_alter_table(
            "intelligence_retrieval_runs",
            recreate="always",
        ) as batch:
            batch.alter_column(
                "representation_sets_json",
                existing_type=sa.Text(),
                nullable=False,
            )
            batch.alter_column(
                "cache_diagnostics_json",
                existing_type=sa.Text(),
                nullable=False,
            )


def _upgrade_caption_candidates() -> None:
    columns: tuple[sa.Column[object], ...] = (
        sa.Column("origin", sa.String(length=40)),
        sa.Column("parent_candidate_id", sa.Integer()),
        sa.Column("created_by", sa.String(length=80)),
        sa.Column("source_proposal_event_id", sa.Integer()),
        sa.Column("derivation_key", sa.String(length=128)),
        sa.Column("feature_schema_version", sa.String(length=80)),
        sa.Column("feature_snapshot_json", sa.Text()),
        sa.Column("feature_snapshot_hash", sa.String(length=64)),
        sa.Column("representation_record_id", sa.Integer()),
        sa.Column("taxonomy_version", sa.String(length=80)),
        sa.Column("verifier_version", sa.String(length=80)),
        sa.Column("ranker_model_version_id", sa.Integer()),
    )
    added = _add_nullable_columns("caption_candidate_records", columns)
    if added:
        op.execute(
            sa.text(
                "UPDATE caption_candidate_records SET origin = 'generated' WHERE origin IS NULL"
            )
        )
        op.execute(
            sa.text(
                "UPDATE caption_candidate_records "
                "SET created_by = 'generator' WHERE created_by IS NULL"
            )
        )
        op.execute(
            sa.text(
                "UPDATE caption_candidate_records "
                "SET feature_snapshot_json = '{}' "
                "WHERE feature_snapshot_json IS NULL"
            )
        )

    needs_batch = bool(added)
    for column, table in (
        ("parent_candidate_id", "caption_candidate_records"),
        ("source_proposal_event_id", "proposal_events"),
        ("representation_record_id", "representation_records"),
        ("ranker_model_version_id", "preference_model_versions"),
    ):
        needs_batch = needs_batch or not _has_foreign_key(
            "caption_candidate_records",
            column,
            table,
        )
    needs_batch = needs_batch or ("derivation_key",) not in _unique_columns(
        "caption_candidate_records"
    )
    if needs_batch:
        with op.batch_alter_table(
            "caption_candidate_records",
            recreate="always",
        ) as batch:
            batch.alter_column(
                "origin",
                existing_type=sa.String(length=40),
                nullable=False,
            )
            batch.alter_column(
                "created_by",
                existing_type=sa.String(length=80),
                nullable=False,
            )
            batch.alter_column(
                "feature_snapshot_json",
                existing_type=sa.Text(),
                nullable=False,
            )
            if not _has_foreign_key(
                "caption_candidate_records",
                "parent_candidate_id",
                "caption_candidate_records",
            ):
                batch.create_foreign_key(
                    "fk_caption_candidate_parent",
                    "caption_candidate_records",
                    ["parent_candidate_id"],
                    ["id"],
                )
            if not _has_foreign_key(
                "caption_candidate_records",
                "source_proposal_event_id",
                "proposal_events",
            ):
                batch.create_foreign_key(
                    "fk_caption_candidate_source_event",
                    "proposal_events",
                    ["source_proposal_event_id"],
                    ["id"],
                )
            if not _has_foreign_key(
                "caption_candidate_records",
                "representation_record_id",
                "representation_records",
            ):
                batch.create_foreign_key(
                    "fk_caption_candidate_representation",
                    "representation_records",
                    ["representation_record_id"],
                    ["id"],
                )
            if not _has_foreign_key(
                "caption_candidate_records",
                "ranker_model_version_id",
                "preference_model_versions",
            ):
                batch.create_foreign_key(
                    "fk_caption_candidate_ranker_model",
                    "preference_model_versions",
                    ["ranker_model_version_id"],
                    ["id"],
                )
            if ("derivation_key",) not in _unique_columns("caption_candidate_records"):
                batch.create_unique_constraint(
                    "uq_caption_candidate_derivation",
                    ["derivation_key"],
                )

    desired_indexes = {
        "ix_caption_candidate_records_origin": ["origin"],
        "ix_caption_candidate_records_parent_candidate_id": ["parent_candidate_id"],
        "ix_caption_candidate_records_source_proposal_event_id": ["source_proposal_event_id"],
        "ix_caption_candidate_records_feature_schema_version": ["feature_schema_version"],
        "ix_caption_candidate_records_feature_snapshot_hash": ["feature_snapshot_hash"],
    }
    existing_indexes = _index_names("caption_candidate_records")
    for name, fields in desired_indexes.items():
        if name not in existing_indexes:
            op.create_index(name, "caption_candidate_records", fields)


def _upgrade_pairwise_preferences() -> None:
    columns: tuple[sa.Column[object], ...] = (
        sa.Column("target", sa.String(length=40)),
        sa.Column("source_proposal_event_id", sa.Integer()),
        sa.Column("source_exposure_id", sa.Integer()),
        sa.Column("source_event_key", sa.String(length=128)),
        sa.Column("derivation_version", sa.String(length=80)),
        sa.Column("idempotency_key", sa.String(length=128)),
        sa.Column("preferred_features_json", sa.Text()),
        sa.Column("dispreferred_features_json", sa.Text()),
        sa.Column("context_snapshot_json", sa.Text()),
        sa.Column("feature_schema_version", sa.String(length=80)),
        sa.Column("feature_snapshot_hash", sa.String(length=64)),
        sa.Column("group_key", sa.String(length=128)),
        sa.Column("taxonomy_version", sa.String(length=80)),
        sa.Column("verifier_version", sa.String(length=80)),
        sa.Column("style_profile_version", sa.Integer()),
        sa.Column("representation_sets_json", sa.Text()),
        sa.Column("retrieval_configuration_hash", sa.String(length=64)),
        sa.Column("ranker_configuration_hash", sa.String(length=64)),
        sa.Column("learning_split", sa.String(length=40)),
        sa.Column("source_study_response_id", sa.Integer()),
    )
    added = _add_nullable_columns("pairwise_preferences", columns)
    if added:
        for column, value in (
            ("target", "caption"),
            ("derivation_version", "decision-derivation-v1"),
            ("preferred_features_json", "{}"),
            ("dispreferred_features_json", "{}"),
            ("context_snapshot_json", "{}"),
            ("representation_sets_json", "{}"),
            ("learning_split", "development"),
        ):
            op.execute(
                sa.text(
                    f"UPDATE pairwise_preferences SET {column} = :value WHERE {column} IS NULL"
                ).bindparams(value=value)
            )

    needs_batch = bool(added)
    needs_batch = needs_batch or not _has_foreign_key(
        "pairwise_preferences",
        "source_proposal_event_id",
        "proposal_events",
    )
    needs_batch = needs_batch or not _has_foreign_key(
        "pairwise_preferences",
        "source_exposure_id",
        "caption_exposures",
    )
    needs_batch = needs_batch or not _has_foreign_key(
        "pairwise_preferences",
        "source_study_response_id",
        "blind_study_responses",
    )
    needs_batch = needs_batch or ("idempotency_key",) not in _unique_columns("pairwise_preferences")
    if needs_batch:
        with op.batch_alter_table(
            "pairwise_preferences",
            recreate="always",
        ) as batch:
            for column, existing_type in (
                ("target", sa.String(length=40)),
                ("derivation_version", sa.String(length=80)),
                ("preferred_features_json", sa.Text()),
                ("dispreferred_features_json", sa.Text()),
                ("context_snapshot_json", sa.Text()),
                ("representation_sets_json", sa.Text()),
                ("learning_split", sa.String(length=40)),
            ):
                batch.alter_column(
                    column,
                    existing_type=existing_type,
                    nullable=False,
                )
            if not _has_foreign_key(
                "pairwise_preferences",
                "source_proposal_event_id",
                "proposal_events",
            ):
                batch.create_foreign_key(
                    "fk_pairwise_source_event",
                    "proposal_events",
                    ["source_proposal_event_id"],
                    ["id"],
                )
            if not _has_foreign_key(
                "pairwise_preferences",
                "source_exposure_id",
                "caption_exposures",
            ):
                batch.create_foreign_key(
                    "fk_pairwise_source_exposure",
                    "caption_exposures",
                    ["source_exposure_id"],
                    ["id"],
                )
            if not _has_foreign_key(
                "pairwise_preferences",
                "source_study_response_id",
                "blind_study_responses",
            ):
                batch.create_foreign_key(
                    "fk_pairwise_source_study_response",
                    "blind_study_responses",
                    ["source_study_response_id"],
                    ["id"],
                )
            if ("idempotency_key",) not in _unique_columns("pairwise_preferences"):
                batch.create_unique_constraint(
                    "uq_pairwise_preference_idempotency",
                    ["idempotency_key"],
                )

    desired_indexes = {
        "ix_pairwise_preferences_target": ["target"],
        "ix_pairwise_preferences_source_proposal_event_id": ["source_proposal_event_id"],
        "ix_pairwise_preferences_source_exposure_id": ["source_exposure_id"],
        "ix_pairwise_preferences_source_event_key": ["source_event_key"],
        "ix_pairwise_preferences_feature_schema_version": ["feature_schema_version"],
        "ix_pairwise_preferences_feature_snapshot_hash": ["feature_snapshot_hash"],
        "ix_pairwise_preferences_group_key": ["group_key"],
        "ix_pairwise_preferences_learning_split": ["learning_split"],
        "ix_pairwise_preferences_source_study_response_id": ["source_study_response_id"],
        "ix_pairwise_preference_training": [
            "channel_id",
            "target",
            "learning_split",
            "feature_schema_version",
            "created_at",
        ],
    }
    existing_indexes = _index_names("pairwise_preferences")
    for name, fields in desired_indexes.items():
        if name not in existing_indexes:
            op.create_index(name, "pairwise_preferences", fields)


def _upgrade_feedback_signals() -> None:
    columns: tuple[sa.Column[object], ...] = (
        sa.Column("source_proposal_event_id", sa.Integer()),
        sa.Column("source_caption_feedback_id", sa.Integer()),
        sa.Column("source_event_key", sa.String(length=128)),
        sa.Column("derivation_version", sa.String(length=80)),
        sa.Column("idempotency_key", sa.String(length=128)),
    )
    added = _add_nullable_columns("feedback_signals", columns)
    if added:
        op.execute(
            sa.text(
                "UPDATE feedback_signals "
                "SET derivation_version = 'feedback-normalization-v1' "
                "WHERE derivation_version IS NULL"
            )
        )

    needs_batch = bool(added)
    needs_batch = needs_batch or not _has_foreign_key(
        "feedback_signals",
        "source_proposal_event_id",
        "proposal_events",
    )
    needs_batch = needs_batch or not _has_foreign_key(
        "feedback_signals",
        "source_caption_feedback_id",
        "caption_feedback",
    )
    needs_batch = needs_batch or ("idempotency_key",) not in _unique_columns("feedback_signals")
    if needs_batch:
        with op.batch_alter_table(
            "feedback_signals",
            recreate="always",
        ) as batch:
            batch.alter_column(
                "derivation_version",
                existing_type=sa.String(length=80),
                nullable=False,
            )
            if not _has_foreign_key(
                "feedback_signals",
                "source_proposal_event_id",
                "proposal_events",
            ):
                batch.create_foreign_key(
                    "fk_feedback_source_event",
                    "proposal_events",
                    ["source_proposal_event_id"],
                    ["id"],
                )
            if not _has_foreign_key(
                "feedback_signals",
                "source_caption_feedback_id",
                "caption_feedback",
            ):
                batch.create_foreign_key(
                    "fk_feedback_source_legacy",
                    "caption_feedback",
                    ["source_caption_feedback_id"],
                    ["id"],
                )
            if ("idempotency_key",) not in _unique_columns("feedback_signals"):
                batch.create_unique_constraint(
                    "uq_feedback_signal_idempotency",
                    ["idempotency_key"],
                )

    desired_indexes = {
        "ix_feedback_signals_source_proposal_event_id": ["source_proposal_event_id"],
        "ix_feedback_signals_source_caption_feedback_id": ["source_caption_feedback_id"],
        "ix_feedback_signals_source_event_key": ["source_event_key"],
    }
    existing_indexes = _index_names("feedback_signals")
    for name, fields in desired_indexes.items():
        if name not in existing_indexes:
            op.create_index(name, "feedback_signals", fields)


def _upgrade_generation_lineage() -> None:
    generation_added = _add_nullable_columns(
        "image_generation_runs",
        (sa.Column("agent_run_id", sa.Integer()),),
    )
    if generation_added or not _has_foreign_key(
        "image_generation_runs",
        "agent_run_id",
        "intelligence_agent_runs",
    ):
        with op.batch_alter_table(
            "image_generation_runs",
            recreate="always",
        ) as batch:
            if not _has_foreign_key(
                "image_generation_runs",
                "agent_run_id",
                "intelligence_agent_runs",
            ):
                batch.create_foreign_key(
                    "fk_image_generation_agent_run",
                    "intelligence_agent_runs",
                    ["agent_run_id"],
                    ["id"],
                )
    if "ix_image_generation_runs_agent_run_id" not in _index_names("image_generation_runs"):
        op.create_index(
            "ix_image_generation_runs_agent_run_id",
            "image_generation_runs",
            ["agent_run_id"],
        )

    lineage_added = _add_nullable_columns(
        "generated_asset_lineage",
        (
            sa.Column("candidate_image_id", sa.Integer()),
            sa.Column("review_status", sa.String(length=40)),
        ),
    )
    if "review_status" in lineage_added:
        op.execute(
            sa.text(
                "UPDATE generated_asset_lineage "
                "SET review_status = 'pending' WHERE review_status IS NULL"
            )
        )
    needs_batch = bool(lineage_added)
    needs_batch = needs_batch or not _has_foreign_key(
        "generated_asset_lineage",
        "candidate_image_id",
        "candidate_images",
    )
    if needs_batch:
        with op.batch_alter_table(
            "generated_asset_lineage",
            recreate="always",
        ) as batch:
            batch.alter_column(
                "review_status",
                existing_type=sa.String(length=40),
                nullable=False,
            )
            if not _has_foreign_key(
                "generated_asset_lineage",
                "candidate_image_id",
                "candidate_images",
            ):
                batch.create_foreign_key(
                    "fk_generated_lineage_candidate",
                    "candidate_images",
                    ["candidate_image_id"],
                    ["id"],
                )
    for name, fields in {
        "ix_generated_asset_lineage_candidate_image_id": ["candidate_image_id"],
        "ix_generated_asset_lineage_review_status": ["review_status"],
    }.items():
        if name not in _index_names("generated_asset_lineage"):
            op.create_index(name, "generated_asset_lineage", fields)


def _normalize_legacy_server_defaults() -> None:
    """Converge historical upgrades with the current 0001 bootstrap schema."""
    with op.batch_alter_table("proposals", recreate="always") as batch:
        batch.alter_column(
            "caption_rationale",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=None,
        )
        batch.alter_column(
            "caption_reference_post_ids_json",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=None,
        )
    with op.batch_alter_table("caption_feedback", recreate="always") as batch:
        batch.alter_column(
            "reason_codes_json",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=None,
        )
    with op.batch_alter_table("publish_attempts", recreate="always") as batch:
        batch.alter_column(
            "screenshot_paths_json",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=None,
        )


def upgrade() -> None:
    _create_representation_tables()
    _create_agent_tables()
    _create_preference_tables()
    _create_annotation_tables()
    _create_study_tables()
    _upgrade_retrieval_runs()
    _upgrade_caption_candidates()
    _upgrade_pairwise_preferences()
    _upgrade_feedback_signals()
    _upgrade_generation_lineage()
    _normalize_legacy_server_defaults()


def _drop_indexes(table: str, names: Iterable[str]) -> None:
    existing = _index_names(table)
    for name in names:
        if name in existing:
            op.drop_index(name, table_name=table)


def _drop_columns(
    table: str,
    columns: Iterable[str],
    *,
    unique_constraints: Iterable[str] = (),
) -> None:
    existing = _column_names(table)
    selected = [column for column in columns if column in existing]
    if not selected:
        return
    existing_uniques = {
        str(constraint["name"])
        for constraint in _inspector().get_unique_constraints(table)
        if constraint.get("name")
    }
    with op.batch_alter_table(table, recreate="always") as batch:
        for constraint in unique_constraints:
            if constraint in existing_uniques:
                batch.drop_constraint(constraint, type_="unique")
        for column in selected:
            batch.drop_column(column)


def _restore_legacy_server_defaults() -> None:
    with op.batch_alter_table("proposals", recreate="always") as batch:
        batch.alter_column(
            "caption_rationale",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=sa.text("''"),
        )
        batch.alter_column(
            "caption_reference_post_ids_json",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=sa.text("'[]'"),
        )
    with op.batch_alter_table("caption_feedback", recreate="always") as batch:
        batch.alter_column(
            "reason_codes_json",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=sa.text("'[]'"),
        )
    with op.batch_alter_table("publish_attempts", recreate="always") as batch:
        batch.alter_column(
            "screenshot_paths_json",
            existing_type=sa.Text(),
            existing_nullable=False,
            server_default=sa.text("'[]'"),
        )


def downgrade() -> None:
    _drop_indexes(
        "generated_asset_lineage",
        (
            "ix_generated_asset_lineage_candidate_image_id",
            "ix_generated_asset_lineage_review_status",
        ),
    )
    _drop_columns(
        "generated_asset_lineage",
        ("candidate_image_id", "review_status"),
    )
    _drop_indexes(
        "image_generation_runs",
        ("ix_image_generation_runs_agent_run_id",),
    )
    _drop_columns("image_generation_runs", ("agent_run_id",))

    _drop_indexes(
        "feedback_signals",
        (
            "ix_feedback_signals_source_proposal_event_id",
            "ix_feedback_signals_source_caption_feedback_id",
            "ix_feedback_signals_source_event_key",
        ),
    )
    _drop_columns(
        "feedback_signals",
        (
            "source_proposal_event_id",
            "source_caption_feedback_id",
            "source_event_key",
            "derivation_version",
            "idempotency_key",
        ),
        unique_constraints=("uq_feedback_signal_idempotency",),
    )
    _drop_indexes(
        "pairwise_preferences",
        (
            "ix_pairwise_preferences_target",
            "ix_pairwise_preferences_source_proposal_event_id",
            "ix_pairwise_preferences_source_exposure_id",
            "ix_pairwise_preferences_source_event_key",
            "ix_pairwise_preferences_feature_schema_version",
            "ix_pairwise_preferences_feature_snapshot_hash",
            "ix_pairwise_preferences_group_key",
            "ix_pairwise_preferences_learning_split",
            "ix_pairwise_preferences_source_study_response_id",
            "ix_pairwise_preference_training",
        ),
    )
    _drop_columns(
        "pairwise_preferences",
        (
            "target",
            "source_proposal_event_id",
            "source_exposure_id",
            "source_event_key",
            "derivation_version",
            "idempotency_key",
            "preferred_features_json",
            "dispreferred_features_json",
            "context_snapshot_json",
            "feature_schema_version",
            "feature_snapshot_hash",
            "group_key",
            "taxonomy_version",
            "verifier_version",
            "style_profile_version",
            "representation_sets_json",
            "retrieval_configuration_hash",
            "ranker_configuration_hash",
            "learning_split",
            "source_study_response_id",
        ),
        unique_constraints=("uq_pairwise_preference_idempotency",),
    )
    _drop_indexes(
        "caption_candidate_records",
        (
            "ix_caption_candidate_records_origin",
            "ix_caption_candidate_records_parent_candidate_id",
            "ix_caption_candidate_records_source_proposal_event_id",
            "ix_caption_candidate_records_feature_schema_version",
            "ix_caption_candidate_records_feature_snapshot_hash",
        ),
    )
    _drop_columns(
        "caption_candidate_records",
        (
            "origin",
            "parent_candidate_id",
            "created_by",
            "source_proposal_event_id",
            "derivation_key",
            "feature_schema_version",
            "feature_snapshot_json",
            "feature_snapshot_hash",
            "representation_record_id",
            "taxonomy_version",
            "verifier_version",
            "ranker_model_version_id",
        ),
        unique_constraints=("uq_caption_candidate_derivation",),
    )
    _drop_columns(
        "intelligence_retrieval_runs",
        ("representation_sets_json", "cache_diagnostics_json"),
    )

    existing = _table_names()
    for table in reversed(NEW_TABLES):
        if table in existing:
            op.drop_table(table)
    _restore_legacy_server_defaults()
