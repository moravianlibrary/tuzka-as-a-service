"""Add jobs.megapixels (for dispatch scoring: engine seconds per megapixel)

Revision ID: 022
Revises: 021
Create Date: 2026-10-21 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "022"
down_revision: str | None = "021"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("megapixels", sa.Float, nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "megapixels")
