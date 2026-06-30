"""Index job_analytics.submitted_at for the time-window analytics queries

Revision ID: 009
Revises: 008
Create Date: 2026-06-29 00:00:00.000000

The dashboard breakdown filters `WHERE submitted_at BETWEEN …` and the raw page does
`ORDER BY submitted_at DESC LIMIT 51`, but the existing indexes are all on stat_date —
so both seq-scanned + sorted the whole fact table. A btree on submitted_at DESC turns
the breakdown into a bounded range scan and the raw page into an instant top-N.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "009"
down_revision: str | None = "008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # IF NOT EXISTS so it's a no-op when the index was created out-of-band (e.g. by the
    # analytics bench harness) — keeps the migration safe to run against any DB.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_job_analytics_submitted_at "
        "ON job_analytics (submitted_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_job_analytics_submitted_at")
