"""Initial Runway schema.

Revision ID: 0001_initial
Revises:
"""

from __future__ import annotations

from alembic import op
from runway.db import models  # noqa: F401
from runway.db.base import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


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
