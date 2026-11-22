"""Add workspaces, users, members, API keys and the audit log; create workspace 1.

Revision ID: 0013_workspaces
Revises: 0012_creator_platforms

Crash safety: env.py runs every revision in one transaction (SQLite DDL
included), and each table and index here is checked individually, so a
database left half-way by an earlier, non-transactional run still upgrades.
"""

from __future__ import annotations

import re
from collections.abc import Callable
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


def _table_names() -> set[str] | None:
    """Existing tables, or None when generating SQL offline (nothing can be read)."""
    if context.is_offline_mode():
        return None
    return set(sa.inspect(op.get_bind()).get_table_names())


def _index_names(table: str) -> set[str]:
    if context.is_offline_mode():
        return set()
    return {str(index["name"]) for index in sa.inspect(op.get_bind()).get_indexes(table)}


def _create_workspaces() -> None:
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


def _create_users() -> None:
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


def _create_workspace_members() -> None:
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


def _create_api_keys() -> None:
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


def _create_audit_log() -> None:
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


# Parents first. Each entry: table, creator, indexes (name, columns).
_TABLES: tuple[tuple[str, Callable[[], None], tuple[tuple[str, list[str]], ...]], ...] = (
    ("workspaces", _create_workspaces, ()),
    ("users", _create_users, ()),
    (
        "workspace_members",
        _create_workspace_members,
        (("ix_workspace_members_user_id", ["user_id"]),),
    ),
    ("api_keys", _create_api_keys, (("ix_api_keys_workspace_id", ["workspace_id"]),)),
    (
        "audit_log",
        _create_audit_log,
        (("ix_audit_log_workspace_created", ["workspace_id", "created_at", "id"]),),
    ),
)


def upgrade() -> None:
    # 0001 builds a fresh database from the current models, so some or all of
    # these may exist already; create exactly what is missing.
    existing = _table_names()
    for table, create, indexes in _TABLES:
        if existing is None or table not in existing:
            create()
        present = _index_names(table) if existing is not None and table in existing else set()
        for index_name, columns in indexes:
            if index_name not in present:
                op.create_index(index_name, table, columns)
    _ensure_workspace_one()


def _ensure_workspace_one() -> None:
    if not context.is_offline_mode():
        found = op.get_bind().execute(sa.text("SELECT 1 FROM workspaces WHERE id = 1")).first()
        if found is not None:
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
    if op.get_context().dialect.name == "postgresql":
        # An explicit id does not advance the serial sequence; without this the
        # next workspace insert would collide with id 1.
        op.execute(
            "SELECT setval(pg_get_serial_sequence('workspaces', 'id'), "
            "(SELECT MAX(id) FROM workspaces))"
        )


def downgrade() -> None:
    existing = _table_names()
    for table in reversed(NEW_TABLES):
        if existing is None or table in existing:
            op.drop_table(table)
