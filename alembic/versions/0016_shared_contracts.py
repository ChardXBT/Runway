"""Add the idempotency-key store and the event log used by /api/v1.

Revision ID: 0016_shared_contracts
Revises: 0015_publish_jobs
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

revision = "0016_shared_contracts"
down_revision = "0015_publish_jobs"
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
    # 0001 builds a fresh database from the current models; create only what is missing.
    existing = _table_names()
    if _missing("idempotency_keys", existing):
        op.create_table(
            "idempotency_keys",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("workspace_id", sa.Integer(), nullable=False),
            sa.Column("principal_id", sa.String(length=200), nullable=False),
            sa.Column("key", sa.String(length=200), nullable=False),
            sa.Column("request_hash", sa.String(length=64), nullable=False),
            sa.Column("response_status", sa.Integer(), nullable=True),
            sa.Column("response_body", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["workspaces.id"], name="fk_idempotency_keys_workspace"
            ),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("workspace_id", "principal_id", "key", name="uq_idempotency_key"),
        )
    _ensure_indexes(
        "idempotency_keys", existing, (("ix_idempotency_keys_created_at", ["created_at"]),)
    )
    if _missing("events", existing):
        op.create_table(
            "events",
            sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
            sa.Column("workspace_id", sa.Integer(), nullable=False),
            sa.Column("type", sa.String(length=80), nullable=False),
            sa.Column("subject_type", sa.String(length=80), nullable=False),
            sa.Column("subject_id", sa.String(length=200), nullable=True),
            sa.Column("payload_json", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(
                ["workspace_id"], ["workspaces.id"], name="fk_events_workspace"
            ),
            sa.PrimaryKeyConstraint("id"),
            # Ids never repeat after a purge, so Last-Event-ID stays meaningful.
            sqlite_autoincrement=True,
        )
    _ensure_indexes(
        "events",
        existing,
        (
            ("ix_events_workspace_id_id", ["workspace_id", "id"]),
            ("ix_events_created_at", ["created_at"]),
        ),
    )


def downgrade() -> None:
    existing = _table_names()
    for table in ("events", "idempotency_keys"):
        if not _missing(table, existing):
            op.drop_table(table)
