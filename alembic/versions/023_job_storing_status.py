"""Allow storing status after engine completion is observed.

Revision ID: 023
Revises: 022
Create Date: 2026-10-22 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "023"
down_revision: str | None = "022"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_jobs_status", "jobs", type_="check")
    op.create_check_constraint(
        "ck_jobs_status", "jobs", "status IN ('queued', 'running', 'storing', 'done', 'failed')"
    )


def downgrade() -> None:
    # A rollback must not strand rows with a value the old constraint rejects.
    op.execute("UPDATE jobs SET status = 'running' WHERE status = 'storing'")
    op.drop_constraint("ck_jobs_status", "jobs", type_="check")
    op.create_check_constraint(
        "ck_jobs_status", "jobs", "status IN ('queued', 'running', 'done', 'failed')"
    )
