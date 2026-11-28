"""Add database-backed Lineup slot claims.

Revision ID: 0013_lineup_slot_claims
Revises: 0012_creator_platforms
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from alembic import op

revision = "0013_lineup_slot_claims"
down_revision = "0012_creator_platforms"
branch_labels = None
depends_on = None

RESERVING_STATUSES = (
    "approved",
    "internally_scheduled",
    "publishing",
    "externally_scheduled",
    "publish_unverified",
    "published",
    "publish_failed",
)


def _table_names() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    if "lineup_slot_claims" not in _table_names():
        op.create_table(
            "lineup_slot_claims",
            sa.Column("id", sa.Integer(), nullable=False),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            sa.Column("proposal_id", sa.Integer(), nullable=False),
            sa.Column("slot_date", sa.Date(), nullable=False),
            sa.Column("scheduled_publish_at", sa.String(length=40), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.ForeignKeyConstraint(["channel_id"], ["channels.id"]),
            sa.ForeignKeyConstraint(["proposal_id"], ["proposals.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "channel_id",
                "slot_date",
                name="uq_lineup_slot_channel_date",
            ),
            sa.UniqueConstraint("proposal_id", name="uq_lineup_slot_proposal"),
        )

    connection = op.get_bind()
    rows = connection.execute(
        sa.text(
            """
            SELECT
                proposals.id AS proposal_id,
                proposals.channel_id AS channel_id,
                proposals.scheduled_publish_at AS scheduled_publish_at,
                channels.timezone AS timezone
            FROM proposals
            JOIN channels ON channels.id = proposals.channel_id
            WHERE proposals.scheduled_publish_at IS NOT NULL
              AND proposals.status IN :statuses
            ORDER BY proposals.id
            """
        ).bindparams(sa.bindparam("statuses", expanding=True)),
        {"statuses": RESERVING_STATUSES},
    ).mappings()
    now = datetime.now(UTC)
    seen: dict[tuple[int, object], int] = {}
    claims: list[dict[str, object]] = []
    for row in rows:
        raw_timestamp = row["scheduled_publish_at"]
        try:
            parsed = datetime.fromisoformat(str(raw_timestamp))
        except ValueError as exc:
            raise RuntimeError(
                "cannot migrate Lineup slot claims: active proposal "
                f"{row['proposal_id']} has invalid scheduled_publish_at={raw_timestamp!r}"
            ) from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise RuntimeError(
                "cannot migrate Lineup slot claims: active proposal "
                f"{row['proposal_id']} has a timezone-less scheduled_publish_at"
            )
        slot_date = parsed.astimezone(ZoneInfo(str(row["timezone"]))).date()
        key = (int(row["channel_id"]), slot_date)
        prior = seen.get(key)
        if prior is not None:
            raise RuntimeError(
                "cannot migrate Lineup slot claims: active proposals "
                f"{prior} and {row['proposal_id']} already share channel date {slot_date}; "
                "resolve the conflicting rows manually before upgrading"
            )
        seen[key] = int(row["proposal_id"])
        claims.append(
            {
                "channel_id": int(row["channel_id"]),
                "proposal_id": int(row["proposal_id"]),
                "slot_date": slot_date,
                "scheduled_publish_at": str(raw_timestamp),
                "created_at": now,
                "updated_at": now,
            }
        )
    if claims:
        claim_table = sa.table(
            "lineup_slot_claims",
            sa.column("channel_id", sa.Integer),
            sa.column("proposal_id", sa.Integer),
            sa.column("slot_date", sa.Date),
            sa.column("scheduled_publish_at", sa.String),
            sa.column("created_at", sa.DateTime),
            sa.column("updated_at", sa.DateTime),
        )
        connection.execute(claim_table.insert(), claims)


def downgrade() -> None:
    if "lineup_slot_claims" in _table_names():
        op.drop_table("lineup_slot_claims")
