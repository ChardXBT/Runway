"""Add ownership leases to workspace and sync runs.

Revision ID: 0013_job_ownership_leases
Revises: 0012_creator_platforms
"""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import op

revision = "0013_job_ownership_leases"
down_revision = "0012_creator_platforms"
branch_labels = None
depends_on = None

JOB_TABLES = ("search_runs", "generation_runs", "sync_runs")
LEASE_COLUMNS = (
    ("owner_token", sa.String(length=128), True),
    ("heartbeat_at", sa.DateTime(timezone=True), True),
    ("lease_expires_at", sa.DateTime(timezone=True), True),
    ("lease_version", sa.Integer(), False),
)


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _columns(table_name: str) -> set[str]:
    return {str(column["name"]) for column in sa.inspect(op.get_bind()).get_columns(table_name)}


def _indexes(table_name: str) -> set[str]:
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name")
    }


def upgrade() -> None:
    existing_tables = _table_names()
    for table_name in JOB_TABLES:
        if table_name not in existing_tables:
            continue
        existing_columns = _columns(table_name)
        for column_name, column_type, _nullable in LEASE_COLUMNS:
            if column_name in existing_columns:
                continue
            column = sa.Column(column_name, column_type, nullable=True)
            if column_name == "lease_version":
                column = sa.Column(
                    column_name,
                    column_type,
                    nullable=False,
                    server_default="0",
                )
            op.add_column(table_name, column)
        if f"ix_{table_name}_lease_expires_at" not in _indexes(table_name):
            op.create_index(
                f"ix_{table_name}_lease_expires_at",
                table_name,
                ["lease_expires_at"],
                unique=False,
            )

    if "sync_runs" in existing_tables:
        # The previous admission rule could leave several old `running` rows for
        # one account because it ignored stale rows without closing them. A
        # partial unique index cannot be created over that already-invalid state.
        # Keep the newest row as the plausible active lineage and close older
        # duplicates exactly as interrupted work; startup/admission recovery will
        # close the newest row too if its lease is absent or expired.
        op.execute(
            sa.text(
                "UPDATE sync_runs "
                "SET status = 'failed', completed_at = :completed_at, "
                "error_summary = 'interrupted during ownership migration' "
                "WHERE status = 'running' AND id NOT IN ("
                "SELECT MAX(id) FROM sync_runs WHERE status = 'running' GROUP BY account_id"
                ")"
            ).bindparams(completed_at=datetime.now(UTC))
        )
        if "ux_sync_runs_account_running" not in _indexes("sync_runs"):
            op.create_index(
                "ux_sync_runs_account_running",
                "sync_runs",
                ["account_id"],
                unique=True,
                sqlite_where=sa.text("status = 'running'"),
            )


def downgrade() -> None:
    existing_tables = _table_names()
    if "sync_runs" in existing_tables and "ux_sync_runs_account_running" in _indexes("sync_runs"):
        op.drop_index("ux_sync_runs_account_running", table_name="sync_runs")
    for table_name in reversed(JOB_TABLES):
        if table_name not in existing_tables:
            continue
        if f"ix_{table_name}_lease_expires_at" in _indexes(table_name):
            op.drop_index(f"ix_{table_name}_lease_expires_at", table_name=table_name)
        existing_columns = _columns(table_name)
        for column_name, _column_type, _nullable in reversed(LEASE_COLUMNS):
            if column_name in existing_columns:
                op.drop_column(table_name, column_name)
