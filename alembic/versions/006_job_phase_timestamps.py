"""Add jobs.dispatched_at and jobs.stored_at phase timestamps

Revision ID: 006
Revises: 005
Create Date: 2026-06-13 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("dispatched_at", sa.DateTime, nullable=True))
    op.add_column("jobs", sa.Column("stored_at", sa.DateTime, nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "stored_at")
    op.drop_column("jobs", "dispatched_at")
