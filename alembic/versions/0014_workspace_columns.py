"""Give every creator-platform table and `channels` a workspace (default 1).

Revision ID: 0014_workspace_columns
Revises: 0013_workspaces

Existing rows are backfilled to workspace 1 by the column default. The
connected-account uniqueness becomes per workspace, so two workspaces may
connect the same platform account (spec 01 section 4). No isolation is
enforced yet; that is phase P7.

SQLite cannot add a foreign key with ALTER TABLE, so batch mode rebuilds each
table there (copy, drop, rename; ids and data are copied as-is). On Postgres
`recreate="auto"` issues plain ALTER TABLE statements instead: rebuilding
would mean dropping tables other tables reference.

Crash safety: env.py makes each revision one transaction (SQLite DDL
included). A database left by an earlier, non-transactional run may still hold
a `_alembic_tmp_<table>` from an interrupted rebuild; it is dropped before
that table is rebuilt, and tables that already have the column are skipped.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

revision = "0014_workspace_columns"
down_revision = "0013_workspaces"
branch_labels = None
depends_on = None

# Parents before children; the order does not matter for SQLite (foreign keys
# are not enforced during migrations) but keeps a Postgres run predictable.
SCOPED_TABLES = (
    "channels",
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


def _fk_name(table: str) -> str:
    return f"fk_{table}_workspace"


def _index_name(table: str) -> str:
    return f"ix_{table}_workspace_id"


def _has_workspace_column(table: str, *, offline_default: bool) -> bool:
    if context.is_offline_mode():
        return offline_default
    columns = sa.inspect(op.get_bind()).get_columns(table)
    return any(column["name"] == "workspace_id" for column in columns)


def _drop_leftover_batch_table(table: str) -> None:
    """Remove the temp copy an interrupted, non-transactional batch run left behind."""
    if context.is_offline_mode() or op.get_context().dialect.name != "sqlite":
        return
    leftover = f"_alembic_tmp_{table}"
    if leftover in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table(leftover)


def upgrade() -> None:
    for table in SCOPED_TABLES:
        _drop_leftover_batch_table(table)
        # A fresh database built by 0001 from the current models already has it.
        if _has_workspace_column(table, offline_default=False):
            continue
        with op.batch_alter_table(table, recreate="auto") as batch:
            batch.add_column(
                sa.Column(
                    "workspace_id",
                    sa.Integer(),
                    nullable=False,
                    server_default=sa.text("1"),
                )
            )
            batch.create_foreign_key(_fk_name(table), "workspaces", ["workspace_id"], ["id"])
            batch.create_index(_index_name(table), ["workspace_id"])
            if table == "connected_accounts":
                batch.drop_constraint("uq_connected_account_external", type_="unique")
                batch.create_unique_constraint(
                    "uq_connected_account_external",
                    ["workspace_id", "platform", "external_account_id"],
                )


def downgrade() -> None:
    for table in reversed(SCOPED_TABLES):
        _drop_leftover_batch_table(table)
        if not _has_workspace_column(table, offline_default=True):
            continue
        with op.batch_alter_table(table, recreate="auto") as batch:
            if table == "connected_accounts":
                # Fails, by design, if two workspaces connected the same account:
                # the old global constraint cannot hold those rows.
                batch.drop_constraint("uq_connected_account_external", type_="unique")
                batch.create_unique_constraint(
                    "uq_connected_account_external", ["platform", "external_account_id"]
                )
            batch.drop_index(_index_name(table))
            batch.drop_constraint(_fk_name(table), type_="foreignkey")
            batch.drop_column("workspace_id")
