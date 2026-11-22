"""Add the idempotency-key store and the event log used by /api/v1.

Revision ID: 0016_shared_contracts
Revises: 0015_publish_jobs
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016_shared_contracts"
down_revision = "0015_publish_jobs"
branch_labels = None
depends_on = None


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    # 0001 builds a fresh database from the current models; create only what is missing.
    existing = _table_names()
    if "idempotency_keys" not in existing:
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
        op.create_index("ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"])
    if "events" not in existing:
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
        op.create_index("ix_events_workspace_id_id", "events", ["workspace_id", "id"])
        op.create_index("ix_events_created_at", "events", ["created_at"])


def downgrade() -> None:
    existing = _table_names()
    for table in ("events", "idempotency_keys"):
        if table in existing:
            op.drop_table(table)
