"""Publishing engine: posts and targets columns, media, attempts ledger, publisher choice.

Revision ID: 0017_publishing_engine
Revises: 0016h_hosted_identity

Adds the spec 03 section 1 columns to `content_drafts` (posts) and
`draft_targets` (targets), backfills them for existing rows, and creates
`media_assets_v2`, `media_uploads`, `draft_media` (spec 02 section 3),
`target_attempts`, `account_publishers` and `publishing_controls`.

Every step checks for itself, so a fresh database (built by 0001 from the
current models) and a rerun after an interrupted upgrade both skip work that
is already done. New columns are added with plain `ALTER TABLE ... ADD
COLUMN` (none of them carries a foreign key), so no table is rebuilt and no
`_alembic_tmp_*` copy can be left behind. The backfills are idempotent.
"""

from __future__ import annotations

from collections.abc import Callable

import sqlalchemy as sa

from alembic import context, op

revision = "0017_publishing_engine"
down_revision = "0016h_hosted_identity"
branch_labels = None
depends_on = None

# Default post type per platform for targets created before post types existed.
LEGACY_POST_TYPES = {
    "youtube": "youtube_video",
    "instagram": "instagram_image",
    "tiktok": "tiktok_video",
}

NEW_TABLES = (
    "media_assets_v2",
    "media_uploads",
    "draft_media",
    "target_attempts",
    "account_publishers",
    "publishing_controls",
)


def _post_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("source", sa.String(length=20), nullable=False, server_default="human"),
        sa.Column("source_ref", sa.String(length=200), nullable=True),
        sa.Column("ai_generated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("created_by", sa.String(length=200), nullable=True),
    ]


def _target_columns() -> list[sa.Column[object]]:
    return [
        sa.Column("post_type", sa.String(length=40), nullable=True),
        sa.Column("planned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("release_state", sa.String(length=20), nullable=False, server_default="none"),
        sa.Column("released_by", sa.String(length=200), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
        sa.Column("state", sa.String(length=30), nullable=False, server_default="pending"),
        sa.Column("state_reason", sa.Text(), nullable=True),
        sa.Column("external_id", sa.String(length=200), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("publisher", sa.String(length=80), nullable=True),
    ]


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _offline() -> bool:
    """`alembic upgrade --sql`: nothing can be inspected, so emit every statement."""
    return context.is_offline_mode()


def _column_names(table: str) -> set[str]:
    if _offline():
        return set()
    return {str(column["name"]) for column in _inspector().get_columns(table)}


def _index_names(table: str) -> set[str]:
    if _offline():
        return set()
    return {str(index["name"]) for index in _inspector().get_indexes(table)}


def _table_names() -> set[str]:
    if _offline():
        return set()
    return set(_inspector().get_table_names())


def _workspace_column() -> sa.Column[object]:
    return sa.Column("workspace_id", sa.Integer(), nullable=False, server_default=sa.text("1"))


def _workspace_fk(table: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        ["workspace_id"], ["workspaces.id"], name=f"fk_{table}_workspace"
    )


def _create_media_assets_v2() -> None:
    op.create_table(
        "media_assets_v2",
        sa.Column("id", sa.Integer(), nullable=False),
        _workspace_column(),
        sa.Column("kind", sa.String(length=20), nullable=True),
        sa.Column("original_filename", sa.String(length=512), nullable=True),
        sa.Column("content_type", sa.String(length=120), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("storage", sa.String(length=10), nullable=False),
        sa.Column("storage_key", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("rejected_reason", sa.Text(), nullable=True),
        sa.Column("probe_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("poster_key", sa.String(length=300), nullable=True),
        sa.Column("created_by", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("delete_after", sa.DateTime(timezone=True), nullable=True),
        _workspace_fk("media_assets_v2"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key", name="uq_media_assets_v2_storage_key"),
    )


def _create_media_uploads() -> None:
    op.create_table(
        "media_uploads",
        sa.Column("id", sa.Integer(), nullable=False),
        _workspace_column(),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("upload_id", sa.String(length=200), nullable=False),
        sa.Column("part_size", sa.BigInteger(), nullable=False),
        sa.Column("parts_json", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("plan_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        _workspace_fk("media_uploads"),
        sa.ForeignKeyConstraint(
            ["asset_id"], ["media_assets_v2.id"], name="fk_media_uploads_asset"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id", name="uq_media_uploads_asset"),
    )


def _create_draft_media() -> None:
    op.create_table(
        "draft_media",
        sa.Column("id", sa.Integer(), nullable=False),
        _workspace_column(),
        sa.Column("draft_id", sa.Integer(), nullable=False),
        sa.Column("asset_id", sa.Integer(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        _workspace_fk("draft_media"),
        sa.ForeignKeyConstraint(["draft_id"], ["content_drafts.id"], name="fk_draft_media_draft"),
        sa.ForeignKeyConstraint(["asset_id"], ["media_assets_v2.id"], name="fk_draft_media_asset"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("draft_id", "position", name="uq_draft_media_position"),
        sa.UniqueConstraint("draft_id", "asset_id", name="uq_draft_media_asset"),
    )


def _create_target_attempts() -> None:
    op.create_table(
        "target_attempts",
        sa.Column("id", sa.Integer(), nullable=False),
        _workspace_column(),
        sa.Column("target_id", sa.Integer(), nullable=False),
        sa.Column("job_id", sa.Integer(), nullable=True),
        sa.Column("adapter", sa.String(length=80), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False, server_default="send"),
        sa.Column("payload_hash", sa.String(length=64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sa.String(length=30), nullable=False),
        sa.Column("request_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("response_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("external_id", sa.String(length=200), nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("error_detail", sa.Text(), nullable=True),
        sa.Column("resumable_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        _workspace_fk("target_attempts"),
        sa.ForeignKeyConstraint(
            ["target_id"], ["draft_targets.id"], name="fk_target_attempts_target"
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def _create_account_publishers() -> None:
    op.create_table(
        "account_publishers",
        sa.Column("id", sa.Integer(), nullable=False),
        _workspace_column(),
        sa.Column("account_id", sa.Integer(), nullable=False),
        sa.Column("post_type", sa.String(length=40), nullable=False),
        sa.Column("publisher", sa.String(length=80), nullable=False),
        sa.Column("updated_by", sa.String(length=200), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        _workspace_fk("account_publishers"),
        sa.ForeignKeyConstraint(
            ["account_id"], ["connected_accounts.id"], name="fk_account_publishers_account"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", "post_type", name="uq_account_publisher_post_type"),
    )


def _create_publishing_controls() -> None:
    op.create_table(
        "publishing_controls",
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("updated_by", sa.String(length=200), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_publishing_controls_workspace"
        ),
        sa.PrimaryKeyConstraint("workspace_id"),
    )


# Indexes are ensured separately from their tables, so a rerun after an
# interrupted upgrade (table created, index not yet) completes the work.
INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("draft_targets", "ix_draft_targets_state", ("state",)),
    ("media_assets_v2", "ix_media_assets_v2_workspace_id", ("workspace_id",)),
    ("media_assets_v2", "ix_media_assets_v2_status", ("status",)),
    ("media_uploads", "ix_media_uploads_workspace_id", ("workspace_id",)),
    ("draft_media", "ix_draft_media_workspace_id", ("workspace_id",)),
    ("draft_media", "ix_draft_media_draft_id", ("draft_id",)),
    ("draft_media", "ix_draft_media_asset_id", ("asset_id",)),
    ("target_attempts", "ix_target_attempts_workspace_id", ("workspace_id",)),
    ("target_attempts", "ix_target_attempts_target_id_id", ("target_id", "id")),
    ("account_publishers", "ix_account_publishers_workspace_id", ("workspace_id",)),
)

_CREATORS: dict[str, Callable[[], None]] = {
    "media_assets_v2": _create_media_assets_v2,
    "media_uploads": _create_media_uploads,
    "draft_media": _create_draft_media,
    "target_attempts": _create_target_attempts,
    "account_publishers": _create_account_publishers,
    "publishing_controls": _create_publishing_controls,
}


def _backfill_targets() -> None:
    # op.execute renders in offline (--sql) mode as well as running online.
    # The machine's column starts as a copy of the legacy status column.
    op.execute(sa.text("UPDATE draft_targets SET state = status WHERE state <> status"))
    # A target's own planned time starts as its post's schedule.
    op.execute(
        sa.text(
            "UPDATE draft_targets SET planned_at = ("
            "SELECT content_drafts.scheduled_for FROM content_drafts "
            "WHERE content_drafts.id = draft_targets.draft_id) "
            "WHERE planned_at IS NULL"
        )
    )
    for platform, post_type in LEGACY_POST_TYPES.items():
        op.execute(
            sa.text(
                "UPDATE draft_targets SET post_type = :post_type "
                "WHERE post_type IS NULL AND platform = :platform"
            ).bindparams(post_type=post_type, platform=platform)
        )


def upgrade() -> None:
    present = _column_names("content_drafts")
    for column in _post_columns():
        if column.name not in present:
            op.add_column("content_drafts", column)
    present = _column_names("draft_targets")
    for column in _target_columns():
        if column.name not in present:
            op.add_column("draft_targets", column)
    _backfill_targets()

    existing = _table_names()
    for table in NEW_TABLES:
        if table not in existing:
            _CREATORS[table]()
    for table, name, columns in INDEXES:
        if name not in _index_names(table):
            op.create_index(name, table, list(columns))


def downgrade() -> None:
    offline = _offline()
    existing = _table_names()
    for table in reversed(NEW_TABLES):
        if offline or table in existing:
            op.drop_table(table)
    if offline or "ix_draft_targets_state" in _index_names("draft_targets"):
        op.drop_index("ix_draft_targets_state", table_name="draft_targets")
    present = _column_names("draft_targets")
    for column in reversed(_target_columns()):
        if offline or column.name in present:
            op.drop_column("draft_targets", column.name)
    present = _column_names("content_drafts")
    for column in reversed(_post_columns()):
        if offline or column.name in present:
            op.drop_column("content_drafts", column.name)
