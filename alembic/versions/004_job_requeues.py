"""Add jobs.requeues counter + seed jobs.max_requeues config

Revision ID: 004
Revises: 003
Create Date: 2026-06-12 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "004"
down_revision: str | None = "003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("requeues", sa.Integer, nullable=False, server_default="0"))
    op.execute(
        "INSERT INTO config (key, value) VALUES ('jobs.max_requeues', '3') "
        "ON CONFLICT (key) DO NOTHING"
    )


def downgrade() -> None:
    op.execute("DELETE FROM config WHERE key = 'jobs.max_requeues'")
    op.drop_column("jobs", "requeues")
