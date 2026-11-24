"""Initial Runway schema.

Revision ID: 0001_initial
Revises:
"""

from __future__ import annotations

from sqlalchemy import MetaData

from alembic import op
from runway.db import models  # noqa: F401
from runway.db.base import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        Base.metadata.create_all(bind=bind)
        return
    # Elsewhere (Postgres, including `--sql` rendering) create from a private
    # copy: resolving the proposals<->caption_slates foreign-key cycle with
    # ALTER marks the shared model metadata, after which a later SQLite build
    # in the same process would leave those foreign keys out.
    metadata = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(metadata)
    metadata.create_all(bind=bind)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        # `metadata.drop_all` cannot order the DROPs: the models contain a
        # proposals<->caption_slates foreign-key cycle. CASCADE removes each
        # table regardless of constraints other tables still point at it.
        for table in reversed(list(Base.metadata.tables.values())):
            op.execute(f'DROP TABLE "{table.name}" CASCADE')
    else:
        Base.metadata.drop_all(bind=bind)
