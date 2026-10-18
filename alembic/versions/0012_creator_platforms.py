"""Add the creator-platform connection, content, inbox, and sync tables.

Revision ID: 0012_creator_platforms
Revises: 0011_runtime_performance
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012_creator_platforms"
down_revision = "0011_runtime_performance"
branch_labels = None
depends_on = None


NEW_TABLES = (
    "connected_accounts",
    "oauth_states",
    "content_groups",
    "platform_content",
    "content_metric_snapshots",
    "account_metric_snapshots",
    "account_metric_days",
    "comment_records",
    "reply_drafts",
    "platform_actions",
    "sync_runs",
    "sync_cursors",
    "content_drafts",
    "draft_targets",
)


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _table_names()
    if "connected_accounts" not in existing:
        op.create_table(
            "connected_accounts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("platform", sa.String(length=20), nullable=False),
            sa.Column("external_account_id", sa.String(length=200), nullable=False),
            sa.Column("handle", sa.String(length=200), nullable=True),
            sa.Column("display_name", sa.String(length=200), nullable=True),
            sa.Column("avatar_url", sa.Text(), nullable=True),
            sa.Column("profile_url", sa.Text(), nullable=True),
            sa.Column("account_type", sa.String(length=60), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("granted_scopes_json", sa.Text(), nullable=False),
            sa.Column("token_ref", sa.String(length=64), nullable=True),
            sa.Column("connected_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_sync_status", sa.String(length=30), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "platform",
                "external_account_id",
                name="uq_connected_account_external",
            ),
            sa.UniqueConstraint("token_ref"),
        )
        op.create_index("ix_connected_accounts_platform", "connected_accounts", ["platform"])
        op.create_index("ix_connected_accounts_status", "connected_accounts", ["status"])

    if "oauth_states" not in existing:
        op.create_table(
            "oauth_states",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("platform", sa.String(length=20), nullable=False),
            sa.Column("state", sa.String(length=128), nullable=False),
            sa.Column("code_verifier", sa.Text(), nullable=True),
            sa.Column("redirect_uri", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("state"),
        )

    if "content_groups" not in existing:
        op.create_table(
            "content_groups",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=300), nullable=False),
            sa.Column("note", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )

    if "platform_content" not in existing:
        op.create_table(
            "platform_content",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("platform", sa.String(length=20), nullable=False),
            sa.Column("external_id", sa.String(length=200), nullable=False),
            sa.Column("content_type", sa.String(length=30), nullable=False),
            sa.Column("title", sa.Text(), nullable=True),
            sa.Column("caption", sa.Text(), nullable=True),
            sa.Column("permalink", sa.Text(), nullable=True),
            sa.Column("thumbnail_url", sa.Text(), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_seconds", sa.Integer(), nullable=True),
            sa.Column("privacy_status", sa.String(length=30), nullable=True),
            sa.Column("view_count", sa.Integer(), nullable=True),
            sa.Column("like_count", sa.Integer(), nullable=True),
            sa.Column("comment_count", sa.Integer(), nullable=True),
            sa.Column("share_count", sa.Integer(), nullable=True),
            sa.Column("metrics_json", sa.Text(), nullable=False),
            sa.Column("metrics_fetched_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("content_group_id", sa.Integer(), nullable=True),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("raw_json", sa.Text(), nullable=False),
            sa.ForeignKeyConstraint(["account_id"], ["connected_accounts.id"]),
            sa.ForeignKeyConstraint(["content_group_id"], ["content_groups.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("account_id", "external_id", name="uq_platform_content_external"),
        )
        op.create_index("ix_platform_content_account_id", "platform_content", ["account_id"])
        op.create_index("ix_platform_content_platform", "platform_content", ["platform"])
        op.create_index("ix_platform_content_content_type", "platform_content", ["content_type"])
        op.create_index("ix_platform_content_view_count", "platform_content", ["view_count"])
        op.create_index("ix_platform_content_like_count", "platform_content", ["like_count"])
        op.create_index("ix_platform_content_comment_count", "platform_content", ["comment_count"])
        op.create_index("ix_platform_content_share_count", "platform_content", ["share_count"])
        op.create_index(
            "ix_platform_content_content_group_id",
            "platform_content",
            ["content_group_id"],
        )
        op.create_index(
            "ix_platform_content_platform_published",
            "platform_content",
            ["platform", "published_at"],
        )

    if "content_metric_snapshots" not in existing:
        op.create_table(
            "content_metric_snapshots",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("content_id", sa.Integer(), nullable=False),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("metrics_json", sa.Text(), nullable=False),
            sa.ForeignKeyConstraint(["content_id"], ["platform_content.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_content_metric_snapshots_content_id",
            "content_metric_snapshots",
            ["content_id"],
        )
        op.create_index(
            "ix_content_metric_snapshots_content_captured",
            "content_metric_snapshots",
            ["content_id", "captured_at"],
        )

    if "account_metric_snapshots" not in existing:
        op.create_table(
            "account_metric_snapshots",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("followers", sa.Integer(), nullable=True),
            sa.Column("following", sa.Integer(), nullable=True),
            sa.Column("content_count", sa.Integer(), nullable=True),
            sa.Column("total_views", sa.Integer(), nullable=True),
            sa.Column("total_likes", sa.Integer(), nullable=True),
            sa.Column("metrics_json", sa.Text(), nullable=False),
            sa.ForeignKeyConstraint(["account_id"], ["connected_accounts.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_account_metric_snapshots_account_id",
            "account_metric_snapshots",
            ["account_id"],
        )

    if "account_metric_days" not in existing:
        op.create_table(
            "account_metric_days",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("day", sa.Date(), nullable=False),
            sa.Column("metric", sa.String(length=60), nullable=False),
            sa.Column("value", sa.Float(), nullable=False),
            sa.Column("source", sa.String(length=60), nullable=False),
            sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["account_id"], ["connected_accounts.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("account_id", "day", "metric", name="uq_account_metric_day"),
        )
        op.create_index("ix_account_metric_days_account_id", "account_metric_days", ["account_id"])

    if "comment_records" not in existing:
        op.create_table(
            "comment_records",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("platform", sa.String(length=20), nullable=False),
            sa.Column("external_id", sa.String(length=200), nullable=False),
            sa.Column("content_id", sa.Integer(), nullable=True),
            sa.Column("parent_external_id", sa.String(length=200), nullable=True),
            sa.Column("author_name", sa.String(length=200), nullable=True),
            sa.Column("author_external_id", sa.String(length=200), nullable=True),
            sa.Column("author_avatar_url", sa.Text(), nullable=True),
            sa.Column("author_is_owner", sa.Boolean(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("like_count", sa.Integer(), nullable=True),
            sa.Column("reply_count", sa.Integer(), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("platform_updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("has_owner_reply", sa.Boolean(), nullable=False),
            sa.Column("permalink", sa.Text(), nullable=True),
            sa.Column("raw_json", sa.Text(), nullable=False),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["account_id"], ["connected_accounts.id"]),
            sa.ForeignKeyConstraint(["content_id"], ["platform_content.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("account_id", "external_id", name="uq_comment_record_external"),
        )
        op.create_index("ix_comment_records_account_id", "comment_records", ["account_id"])
        op.create_index("ix_comment_records_platform", "comment_records", ["platform"])
        op.create_index("ix_comment_records_content_id", "comment_records", ["content_id"])
        op.create_index(
            "ix_comment_records_parent_external_id",
            "comment_records",
            ["parent_external_id"],
        )
        op.create_index("ix_comment_records_status", "comment_records", ["status"])
        op.create_index(
            "ix_comment_records_has_owner_reply", "comment_records", ["has_owner_reply"]
        )
        op.create_index(
            "ix_comment_records_account_published",
            "comment_records",
            ["account_id", "published_at"],
        )

    if "reply_drafts" not in existing:
        op.create_table(
            "reply_drafts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("comment_id", sa.Integer(), nullable=False),
            sa.Column("text", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["comment_id"], ["comment_records.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("comment_id"),
        )

    if "platform_actions" not in existing:
        op.create_table(
            "platform_actions",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("action_type", sa.String(length=40), nullable=False),
            sa.Column("target_type", sa.String(length=40), nullable=False),
            sa.Column("target_id", sa.Integer(), nullable=True),
            sa.Column("state", sa.String(length=20), nullable=False),
            sa.Column("request_json", sa.Text(), nullable=False),
            sa.Column("response_json", sa.Text(), nullable=False),
            sa.Column("external_result_id", sa.String(length=200), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("attempted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["account_id"], ["connected_accounts.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_platform_actions_account_id", "platform_actions", ["account_id"])
        op.create_index("ix_platform_actions_action_type", "platform_actions", ["action_type"])
        op.create_index("ix_platform_actions_state", "platform_actions", ["state"])

    if "sync_runs" not in existing:
        op.create_table(
            "sync_runs",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("trigger", sa.String(length=20), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("steps_json", sa.Text(), nullable=False),
            sa.Column("items_fetched", sa.Integer(), nullable=False),
            sa.Column("quota_units_estimate", sa.Integer(), nullable=False),
            sa.Column("error_summary", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["account_id"], ["connected_accounts.id"]),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_sync_runs_account_id", "sync_runs", ["account_id"])
        op.create_index("ix_sync_runs_status", "sync_runs", ["status"])

    if "sync_cursors" not in existing:
        op.create_table(
            "sync_cursors",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=False),
            sa.Column("scope", sa.String(length=200), nullable=False),
            sa.Column("cursor_json", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["account_id"], ["connected_accounts.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("account_id", "scope", name="uq_sync_cursor_scope"),
        )
        op.create_index("ix_sync_cursors_account_id", "sync_cursors", ["account_id"])

    if "content_drafts" not in existing:
        op.create_table(
            "content_drafts",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("title", sa.String(length=300), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("media_path", sa.Text(), nullable=True),
            sa.Column("media_kind", sa.String(length=20), nullable=True),
            sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index("ix_content_drafts_scheduled_for", "content_drafts", ["scheduled_for"])
        op.create_index("ix_content_drafts_status", "content_drafts", ["status"])

    if "draft_targets" not in existing:
        op.create_table(
            "draft_targets",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("draft_id", sa.Integer(), nullable=False),
            sa.Column("platform", sa.String(length=20), nullable=False),
            sa.Column("account_id", sa.Integer(), nullable=True),
            sa.Column("caption_override", sa.Text(), nullable=True),
            sa.Column("settings_json", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=30), nullable=False),
            sa.Column("published_content_id", sa.Integer(), nullable=True),
            sa.Column("published_url", sa.Text(), nullable=True),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("error", sa.Text(), nullable=True),
            sa.ForeignKeyConstraint(["draft_id"], ["content_drafts.id"]),
            sa.ForeignKeyConstraint(["account_id"], ["connected_accounts.id"]),
            sa.ForeignKeyConstraint(["published_content_id"], ["platform_content.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("draft_id", "platform", "account_id", name="uq_draft_target"),
        )
        op.create_index("ix_draft_targets_draft_id", "draft_targets", ["draft_id"])
        op.create_index("ix_draft_targets_account_id", "draft_targets", ["account_id"])


def downgrade() -> None:
    existing = _table_names()
    for table_name in reversed(NEW_TABLES):
        if table_name in existing:
            op.drop_table(table_name)
