"""Database token store (spec 05 section 3, work package 7.5).

Revision ID: 0022_account_tokens
Revises: 0021_row_level_security

Adds:

- `account_tokens` - one encrypted platform grant per connected account. The
  row is keyed by `account_id` so account deletion or the dead-account purge
  takes the grant with it; `workspace_id` carries the tenancy column every
  scoped table must have. The ciphertext is AES-256-GCM; which
  `RUNWAY_TOKEN_KEYS` entry produced it is recorded in `key_version` for
  rotation.
- `oauth_states.token_ciphertext` - a token parked mid-flow (Instagram
  unified login), encrypted under the token-store cipher. Kept on the state
  row because it outlives the request but dies with the state's TTL.

`account_tokens` carries `workspace_id`, so on Postgres it also joins the
fail-closed RLS set from `0021_row_level_security`: `ENABLE` + `FORCE` ROW
LEVEL SECURITY and the same `runway_workspace` policy (system sessions mark
themselves `app.system = 'on'`, bound sessions match `app.workspace_id`, a
session with neither GUC sees zero rows). A no-op on SQLite, like 0021.

Every step inspects first, so a fresh database (built by 0001 from the
current models) and a rerun after an interrupted upgrade both skip finished
work. Plain `CREATE TABLE` plus one `ADD COLUMN` - no rebuilds.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

revision = "0022_account_tokens"
down_revision = "0021_row_level_security"
branch_labels = None
depends_on = None

# Tables created by this revision that carry `workspace_id` and must sit under
# row-level security on Postgres. Kept literal for the same reason as 0021's:
# the migration stays stable as models evolve.
SCOPED_TABLES = ("account_tokens",)

POLICY_NAME = "runway_workspace"

# Identical fail-closed policy to 0021: a session marked `app.system = 'on'`
# (emitted by `Database.system_session`) or matching `app.workspace_id` sees
# rows; a session with neither GUC sees zero.
_POLICY_EXPRESSION = (
    "current_setting('app.system', true) = 'on' "
    "OR workspace_id = nullif(current_setting('app.workspace_id', true), '')::int"
)


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _offline() -> bool:
    """`alembic upgrade --sql`: nothing can be inspected, so emit every statement."""
    return context.is_offline_mode()


def _table_names() -> set[str]:
    if _offline():
        return set()
    return set(_inspector().get_table_names())


def _column_names(table: str) -> set[str]:
    if _offline():
        return set()
    return {str(column["name"]) for column in _inspector().get_columns(table)}


def _index_names(table: str) -> set[str]:
    if _offline():
        return set()
    return {str(index["name"]) for index in _inspector().get_indexes(table)}


def _enable_rls(table: str) -> None:
    """0021's policy shape, applied to a table this revision created."""
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(
        f"CREATE POLICY {POLICY_NAME} ON {table} "
        f"USING ({_POLICY_EXPRESSION}) WITH CHECK ({_POLICY_EXPRESSION})"
    )


def _disable_rls(table: str) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(f"DROP POLICY IF EXISTS {POLICY_NAME} ON {table}")
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def upgrade() -> None:
    if _offline() or "account_tokens" not in _table_names():
        # `op.create_table`, not a standalone `sa.Table`: a table built against
        # a private `MetaData` cannot resolve its foreign keys when `create`
        # emits the DDL (the post-downgrade re-upgrade hits exactly that).
        op.create_table(
            "account_tokens",
            sa.Column("account_id", sa.Integer(), primary_key=True),
            # `WorkspaceOwned` gives the model `server_default=text("1")`; the
            # created table must carry the same bare-integer default or a
            # re-upgraded schema drifts.
            sa.Column("workspace_id", sa.Integer(), nullable=False, server_default=sa.text("1")),
            sa.Column("key_version", sa.String(length=20), nullable=False),
            sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
            sa.Column("nonce", sa.LargeBinary(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("scopes_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["account_id"], ["connected_accounts.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"]),
        )
    if _offline() or "ix_account_tokens_workspace_id" not in _index_names("account_tokens"):
        op.create_index("ix_account_tokens_workspace_id", "account_tokens", ["workspace_id"])
    if _offline() or "account_tokens" in _table_names():
        for table in SCOPED_TABLES:
            _enable_rls(table)
    if _offline() or "token_ciphertext" not in _column_names("oauth_states"):
        op.add_column("oauth_states", sa.Column("token_ciphertext", sa.Text(), nullable=True))


def downgrade() -> None:
    if _offline() or "token_ciphertext" in _column_names("oauth_states"):
        op.drop_column("oauth_states", "token_ciphertext")
    if _offline() or "account_tokens" in _table_names():
        for table in SCOPED_TABLES:
            _disable_rls(table)
        op.drop_table("account_tokens")
