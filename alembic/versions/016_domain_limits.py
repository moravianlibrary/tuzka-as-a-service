"""Per-domain upload and running-time limits

Revision ID: 016
Revises: 015
Create Date: 2026-10-20 00:00:00.000000

A map sheet is a much bigger upload and a much longer job than a text page, so the
global limits can't serve both. NULL means the global value (Settings.max_upload_bytes,
jobs.running_timeout_seconds). The maps domain is seeded with its limits (150 MiB,
1800 s) unless it already has some.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "016"
down_revision: str | None = "015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("domains", sa.Column("max_upload_bytes", sa.BigInteger, nullable=True))
    op.add_column("domains", sa.Column("running_timeout_seconds", sa.Integer, nullable=True))
    op.execute(
        """
        INSERT INTO domains (name, label, max_upload_bytes, running_timeout_seconds)
        VALUES ('maps', 'Maps', 157286400, 1800)
        ON CONFLICT (name) DO UPDATE SET
            max_upload_bytes = COALESCE(domains.max_upload_bytes, EXCLUDED.max_upload_bytes),
            running_timeout_seconds =
                COALESCE(domains.running_timeout_seconds, EXCLUDED.running_timeout_seconds)
        """
    )


def downgrade() -> None:
    op.drop_column("domains", "running_timeout_seconds")
    op.drop_column("domains", "max_upload_bytes")
