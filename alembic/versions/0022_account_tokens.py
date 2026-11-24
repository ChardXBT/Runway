"""Database token store (spec 05 section 3, work package 7.5).

Revision ID: 0022_account_tokens
Revises: 0020_youtube_publishing

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

Every step inspects first, so a fresh database (built by 0001 from the
current models) and a rerun after an interrupted upgrade both skip finished
work. Plain `CREATE TABLE` plus one `ADD COLUMN` - no rebuilds.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

revision = "0022_account_tokens"
down_revision = "0020_youtube_publishing"
branch_labels = None
depends_on = None


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


ACCOUNT_TOKENS = sa.Table(
    "account_tokens",
    sa.MetaData(),
    sa.Column(
        "account_id",
        sa.Integer(),
        sa.ForeignKey("connected_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    sa.Column("workspace_id", sa.Integer(), sa.ForeignKey("workspaces.id"), nullable=False),
    sa.Column("key_version", sa.String(length=20), nullable=False),
    sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
    sa.Column("nonce", sa.LargeBinary(), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("refresh_expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("scopes_json", sa.Text(), nullable=False, server_default="[]"),
    sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
)


def upgrade() -> None:
    if _offline() or "account_tokens" not in _table_names():
        ACCOUNT_TOKENS.create(op.get_bind(), checkfirst=True)
    if _offline() or "ix_account_tokens_workspace_id" not in _index_names("account_tokens"):
        op.create_index("ix_account_tokens_workspace_id", "account_tokens", ["workspace_id"])
    if _offline() or "token_ciphertext" not in _column_names("oauth_states"):
        op.add_column("oauth_states", sa.Column("token_ciphertext", sa.Text(), nullable=True))


def downgrade() -> None:
    if _offline() or "token_ciphertext" in _column_names("oauth_states"):
        op.drop_column("oauth_states", "token_ciphertext")
    if _offline() or "account_tokens" in _table_names():
        op.drop_table("account_tokens")
