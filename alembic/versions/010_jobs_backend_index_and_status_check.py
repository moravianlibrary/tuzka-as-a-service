"""Index jobs.backend_id and constrain jobs.status

Revision ID: 010
Revises: 009
Create Date: 2026-07-03 00:00:00.000000

jobs.backend_id is a foreign key joined/filtered in the dashboard and delete-guard
queries but was unindexed. jobs.status was a free string with no constraint, unlike
backends.device (which already has ck_backends_device) — this pins it to the four
lifecycle values so an illegal status can't be written.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "010"
down_revision: str | None = "009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index("ix_jobs_backend_id", "jobs", ["backend_id"])
    op.create_check_constraint(
        "ck_jobs_status", "jobs", "status IN ('queued', 'running', 'done', 'failed')"
    )


def downgrade() -> None:
    op.drop_constraint("ck_jobs_status", "jobs", type_="check")
    op.drop_index("ix_jobs_backend_id", table_name="jobs")
