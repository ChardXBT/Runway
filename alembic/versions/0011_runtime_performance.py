"""Add indexes for graph, Lineup, activity, and exposure hot paths.

Revision ID: 0011_runtime_performance
Revises: 0010_neural_intelligence
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011_runtime_performance"
down_revision = "0010_neural_intelligence"
branch_labels = None
depends_on = None


INDEXES: tuple[tuple[str, str, list[str]], ...] = (
    (
        "ix_similarity_edges_target_post_id",
        "similarity_edges",
        ["target_post_id"],
    ),
    (
        "ix_proposals_channel_created",
        "proposals",
        ["channel_id", "created_at", "id"],
    ),
    (
        "ix_proposals_channel_scheduled",
        "proposals",
        ["channel_id", "scheduled_publish_at", "id"],
    ),
    (
        "ix_audit_events_created",
        "audit_events",
        ["created_at", "id"],
    ),
    (
        "ix_audit_events_type_created",
        "audit_events",
        ["event_type", "created_at", "id"],
    ),
    (
        "ix_candidate_exposure_candidate_event_created",
        "candidate_exposures",
        ["channel_id", "candidate_image_id", "event_type", "created_at", "id"],
    ),
    (
        "ix_publish_attempts_publisher_status_id",
        "publish_attempts",
        ["publisher", "status", "id"],
    ),
    (
        "ix_publish_attempts_proposal_publisher_id",
        "publish_attempts",
        ["proposal_id", "publisher", "id"],
    ),
)


def _indexes(table_name: str) -> set[str]:
    return {
        str(index["name"])
        for index in sa.inspect(op.get_bind()).get_indexes(table_name)
        if index.get("name")
    }


def upgrade() -> None:
    for name, table_name, columns in INDEXES:
        if name not in _indexes(table_name):
            op.create_index(name, table_name, columns, unique=False)


def downgrade() -> None:
    for name, table_name, _columns in reversed(INDEXES):
        if name in _indexes(table_name):
            op.drop_index(name, table_name=table_name)
