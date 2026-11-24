"""Enforce workspace tenancy with Postgres row-level security (spec 07).

Revision ID: 0021_row_level_security
Revises: 0020_youtube_publishing

Every table that carries a `workspace_id` column gets `ENABLE` + `FORCE` ROW
LEVEL SECURITY and one policy, `runway_workspace`:

- Sessions with `SET LOCAL app.system = 'on'` - which `Database.system_session`
  emits on `after_begin` - see every row. So does any role with BYPASSRLS.
- Sessions with `SET LOCAL app.workspace_id = '<id>'` - emitted for every
  workspace-bound session - read and write only their workspace's rows.
  `FORCE` makes even the table owner obey the policy, so the application role
  cannot widen a query it forgot to filter.
- Sessions with neither GUC fail closed: they see zero rows. An unmarked
  session can never widen itself into a system session by omission.

NULL `workspace_id` rows (only `webhook_events` keeps them, as the ingest
buffer before the platform signature resolves a workspace) match no
workspace, so bound sessions never see them and system sessions still do.

The migration is a no-op on SQLite, where the application-layer isolation
(workspace context + loader criteria + the flush guard) is the whole defense.
"""

from __future__ import annotations

from alembic import op

revision = "0021_row_level_security"
down_revision = "0020_youtube_publishing"
branch_labels = None
depends_on = None

# Every table with a workspace_id column at this revision. Kept as a literal
# list (not read from the models) so the migration stays stable as models
# evolve; later tables get their own RLS migration.
SCOPED_TABLES = (
    "account_metric_days",
    "account_metric_snapshots",
    "account_publishers",
    "account_sync_state",
    "api_key_release_grants",
    "api_keys",
    "audit_log",
    "channels",
    "comment_records",
    "connected_accounts",
    "content_drafts",
    "content_groups",
    "content_metric_snapshots",
    "draft_media",
    "draft_targets",
    "events",
    "idempotency_keys",
    "media_assets_v2",
    "media_uploads",
    "oauth_states",
    "platform_actions",
    "platform_content",
    "platform_subscriptions",
    "publish_jobs",
    "publishing_controls",
    "reply_drafts",
    "sync_cursors",
    "sync_runs",
    "target_attempts",
    "webhook_deliveries",
    "webhook_endpoints",
    "webhook_events",
    "workspace_members",
    "workspace_policies",
)

POLICY_NAME = "runway_workspace"

# Fail closed: rows are visible to a session that marked itself a system
# session (`app.system = 'on'`, emitted by `Database.system_session`) or whose
# `app.workspace_id` equals the row's tenant. A session with neither GUC sees
# zero rows. NULLIF(...)::int cannot fail on '' because NULLIF makes it NULL.
_POLICY_EXPRESSION = (
    "current_setting('app.system', true) = 'on' "
    "OR workspace_id = nullif(current_setting('app.workspace_id', true), '')::int"
)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in SCOPED_TABLES:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {POLICY_NAME} ON {table} "
            f"USING ({_POLICY_EXPRESSION}) WITH CHECK ({_POLICY_EXPRESSION})"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for table in SCOPED_TABLES:
        op.execute(f"DROP POLICY IF EXISTS {POLICY_NAME} ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")
