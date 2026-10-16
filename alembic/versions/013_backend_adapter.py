"""Add backends.adapter column

Revision ID: 013
Revises: 012
Create Date: 2026-10-16 00:00:00.000000

Every backend has an adapter id that selects which engine protocol to use.
The default is "tuzkaocr"; new backends are always set explicitly (admin API, CLI,
or Helm register hook).
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "013"
down_revision: str | None = "012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "backends",
        sa.Column("adapter", sa.Text, nullable=False, server_default="tuzkaocr"),
    )


def downgrade() -> None:
    op.drop_column("backends", "adapter")
