"""Job TTLs + results eviction: job_results.size_bytes / evicted_at, a TTL index

Revision ID: 019
Revises: 018
Create Date: 2026-10-21 00:00:00.000000

size_bytes (compressed, as stored) lets the cleanup worker evict the oldest results
under a storage cap with one query instead of a bucket listing; evicted_at marks a
result whose object is gone (its endpoints answer 410). Rows from before this revision
have no size and are evicted by age only.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "019"
down_revision: str | None = "018"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("job_results", sa.Column("size_bytes", sa.BigInteger, nullable=True))
    op.add_column("job_results", sa.Column("evicted_at", sa.DateTime, nullable=True))
    # The TTL sweep: finished jobs of one status, oldest first.
    op.create_index("ix_jobs_status_finished", "jobs", ["status", "finished_at"])


def downgrade() -> None:
    op.drop_index("ix_jobs_status_finished", "jobs")
    op.drop_column("job_results", "evicted_at")
    op.drop_column("job_results", "size_bytes")
