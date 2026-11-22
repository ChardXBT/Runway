"""Add workspaces, users, members, API keys and the audit log; create workspace 1.

Revision ID: 0013_workspaces
Revises: 0012_creator_platforms
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import sqlalchemy as sa

from alembic import context, op

revision = "0013_workspaces"
down_revision = "0012_creator_platforms"
branch_labels = None
depends_on = None

NEW_TABLES = ("workspaces", "users", "workspace_members", "api_keys", "audit_log")


def _channel_name() -> str:
    # runway.db.bootstrap passes the configured channel name; a bare `alembic
    # upgrade` falls back to the same settings the application would load.
    configured = context.config.attributes.get("runway_channel_name")
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    from runway.config import get_settings

    return get_settings().channel_name


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.casefold()).strip("-")
    return slug[:80] or "workspace-1"


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    # 0001 builds a fresh database from the current models, so these tables
    # may already exist; only the missing ones are created (as 0004-0012 do).
    existing = _table_names()
    if "workspaces" not in existing:
        _create_workspace_tables()
    _ensure_workspace_one()


def _create_workspace_tables() -> None:
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("profile_note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug", name="uq_workspace_slug"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("external_id", sa.String(length=200), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=True),
        sa.Column("display_name", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("external_id", name="uq_user_external_id"),
    )
    op.create_table(
        "workspace_members",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"], ["workspaces.id"], name="fk_workspace_members_workspace"
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name="fk_workspace_members_user"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_workspace_member"),
    )
    op.create_index("ix_workspace_members_user_id", "workspace_members", ["user_id"])
    op.create_table(
        "api_keys",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("prefix", sa.String(length=40), nullable=False),
        sa.Column("key_hash", sa.String(length=128), nullable=False),
        sa.Column("scopes_json", sa.Text(), nullable=False),
        sa.Column("created_by", sa.String(length=200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_api_keys_workspace"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("prefix", name="uq_api_key_prefix"),
        sa.UniqueConstraint("key_hash", name="uq_api_key_hash"),
    )
    op.create_index("ix_api_keys_workspace_id", "api_keys", ["workspace_id"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("workspace_id", sa.Integer(), nullable=False),
        sa.Column("actor_kind", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.String(length=200), nullable=False),
        sa.Column("action", sa.String(length=80), nullable=False),
        sa.Column("target_type", sa.String(length=80), nullable=True),
        sa.Column("target_id", sa.String(length=200), nullable=True),
        sa.Column("detail_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], name="fk_audit_log_workspace"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_log_workspace_created", "audit_log", ["workspace_id", "created_at", "id"]
    )


def _ensure_workspace_one() -> None:
    bind = op.get_bind()
    if bind.execute(sa.text("SELECT 1 FROM workspaces WHERE id = 1")).first() is not None:
        return
    name = _channel_name()
    workspaces = sa.table(
        "workspaces",
        sa.column("id", sa.Integer()),
        sa.column("slug", sa.String()),
        sa.column("name", sa.String()),
        sa.column("profile_note", sa.Text()),
        sa.column("created_at", sa.DateTime(timezone=True)),
    )
    op.bulk_insert(
        workspaces,
        [
            {
                "id": 1,
                "slug": _slug(name),
                "name": name,
                "profile_note": "local owner workspace created by migration 0013",
                "created_at": datetime.now(UTC),
            }
        ],
    )


def downgrade() -> None:
    existing = _table_names()
    for table_name in reversed(NEW_TABLES):
        if table_name in existing:
            op.drop_table(table_name)
