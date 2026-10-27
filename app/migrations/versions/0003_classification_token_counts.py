"""classification token counts

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-27 19:13:46.696528
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("classifications", schema=None) as batch_op:
        batch_op.add_column(sa.Column("input_tokens", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("output_tokens", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("classifications", schema=None) as batch_op:
        batch_op.drop_column("output_tokens")
        batch_op.drop_column("input_tokens")
