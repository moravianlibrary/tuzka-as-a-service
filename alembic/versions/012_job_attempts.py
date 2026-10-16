"""Add jobs.attempts JSONB column

Revision ID: 012
Revises: 011
Create Date: 2026-10-16 00:00:00.000000

Every failed attempt on a job is recorded as a JSONB object so that the
full retry history survives to the database (and later the CLI, step 6).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision: str = "012"
down_revision: str | None = "011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "attempts",
            JSONB,
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("jobs", "attempts")
