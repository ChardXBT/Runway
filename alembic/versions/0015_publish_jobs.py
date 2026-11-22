"""Add the durable job queue used by `runway worker` (spec 03 section 3).

Revision ID: 0015_publish_jobs
Revises: 0014_workspace_columns
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

revision = "0015_publish_jobs"
down_revision = "0014_workspace_columns"
branch_labels = None
depends_on = None


def _table_names() -> set[str] | None:
    """Existing tables, or None when generating SQL offline (nothing can be read)."""
    if context.is_offline_mode():
        return None
    return set(sa.inspect(op.get_bind()).get_table_names())


def _missing(table: str, existing: set[str] | None) -> bool:
    return existing is None or table not in existing


def _ensure_indexes(
    table: str, existing: set[str] | None, indexes: tuple[tuple[str, list[str]], ...]
) -> None:
    # Checked one by one, so a table created by an earlier partial run still
    # gets every index.
    present: set[str] = set()
    if existing is not None and table in existing:
        present = {str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(table)}
    for name, columns in indexes:
        if name not in present:
            op.create_index(name, table, columns)


def upgrade() -> None:
    existing = _table_names()
    if _missing("publish_jobs", existing):
        _create_publish_jobs()
    _ensure_indexes(
        "publish_jobs",
        existing,
        (
            ("ix_publish_jobs_state_run_at", ["state", "run_at"]),
            ("ix_publish_jobs_workspace_id", ["workspace_id"]),
            ("ix_publish_jobs_target_id", ["target_id"]),
        ),
    )


def _create_publish_jobs() -> None:
    op.create_table(
        "publish_jobs",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False, server_default=sa.text("1")),
        # Null for kinds that do not publish (sync_account, purge, ...).
        sa.Column("target_id", sa.Integer(), nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("run_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default=sa.text("3")),
        sa.Column("locked_by", sa.String(length=200), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("idempotency_key", sa.String(length=300), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_publish_jobs_workspace"
        ),
        sa.ForeignKeyConstraint(["target_id"], ["draft_targets.id"], name="fk_publish_jobs_target"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key", name="uq_publish_job_idempotency"),
    )


def downgrade() -> None:
    if not _missing("publish_jobs", _table_names()):
        op.drop_table("publish_jobs")
