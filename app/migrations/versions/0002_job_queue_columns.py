"""job queue columns

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23 05:16:37.189738
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Events that were unfinished before this migration are left unscheduled
    # (next_attempt_at NULL), so upgrading never re-sends old work on its own.
    # Use scripts/replay_events.py to queue them.
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.add_column(sa.Column("attempts", sa.Integer(), server_default="0", nullable=False))
        batch_op.add_column(sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_events_next_attempt_at"), ["next_attempt_at"], unique=False
        )


def downgrade() -> None:
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_events_next_attempt_at"))
        batch_op.drop_column("locked_until")
        batch_op.drop_column("next_attempt_at")
        batch_op.drop_column("attempts")
