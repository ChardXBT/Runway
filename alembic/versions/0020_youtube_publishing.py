"""Bind an OAuth state row to the account it was issued for (spec 08 section 1).

Revision ID: 0020_youtube_publishing
Revises: 0017_publishing_engine

Adds two columns to `oauth_states`:

- `purpose` - why the flow was started: `connect` (default; a new account or
  a plain reconnect), `reconnect` (re-attach an existing account) or
  `enable_publishing` (incremental consent that adds the YouTube upload
  scope to an existing account).
- `account_id` - the connected account the flow was issued for. When set,
  the callback must attach the token to exactly this row and must refuse a
  token whose channel id differs (the wrong-channel guard).

Existing rows keep `purpose='connect'` and `account_id=NULL`, which the
callback treats exactly like the old unbound flow.

Every step checks for itself, so a fresh database (built by 0001 from the
current models, which now include the columns) and a rerun after an
interrupted upgrade both skip work that is already done. Plain `ALTER TABLE
... ADD COLUMN` - no foreign keys, no rebuild.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import context, op

revision = "0020_youtube_publishing"
down_revision = "0017_publishing_engine"
branch_labels = None
depends_on = None


def _inspector() -> sa.Inspector:
    return sa.inspect(op.get_bind())


def _offline() -> bool:
    """`alembic upgrade --sql`: nothing can be inspected, so emit every statement."""
    return context.is_offline_mode()


def _column_names(table: str) -> set[str]:
    if _offline():
        return set()
    return {str(column["name"]) for column in _inspector().get_columns(table)}


NEW_COLUMNS = (
    sa.Column("purpose", sa.String(length=30), nullable=False, server_default=sa.text("'connect'")),
    sa.Column("account_id", sa.Integer(), nullable=True),
)


def upgrade() -> None:
    present = _column_names("oauth_states")
    for column in NEW_COLUMNS:
        if column.name not in present:
            op.add_column("oauth_states", column)


def downgrade() -> None:
    present = _column_names("oauth_states")
    for column in reversed(NEW_COLUMNS):
        if column.name in present:
            op.drop_column("oauth_states", column.name)
