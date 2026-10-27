"""helpdesk tickets and replies

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-27 22:51:57.617201
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "replies",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("event_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("author", sa.String(length=100), nullable=False),
        sa.Column("model", sa.String(length=64), nullable=True),
        sa.Column("needs_human", sa.Boolean(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["event_id"],
            ["events.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    with op.batch_alter_table("replies", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("ix_replies_event_id"), ["event_id"], unique=False)

    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("ticket_status", sa.String(length=16), server_default="open", nullable=False)
        )
        batch_op.add_column(sa.Column("assignee", sa.String(length=100), nullable=True))
        batch_op.add_column(
            sa.Column("first_response_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("escalated_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.create_index(
            batch_op.f("ix_events_ticket_status"), ["ticket_status"], unique=False
        )

    # Events from before the helpdesk were handled in Slack; start them as resolved so the
    # new inbox only shows what arrives from now on. Any of them can be reopened.
    op.execute("UPDATE events SET ticket_status = 'resolved'")


def downgrade() -> None:
    with op.batch_alter_table("events", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_events_ticket_status"))
        batch_op.drop_column("escalated_at")
        batch_op.drop_column("resolved_at")
        batch_op.drop_column("first_response_at")
        batch_op.drop_column("assignee")
        batch_op.drop_column("ticket_status")

    with op.batch_alter_table("replies", schema=None) as batch_op:
        batch_op.drop_index(batch_op.f("ix_replies_event_id"))

    op.drop_table("replies")
