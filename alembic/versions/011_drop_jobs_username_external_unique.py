"""Drop the (username, external_id) unique constraint on jobs

Revision ID: 011
Revises: 010
Create Date: 2026-07-16 00:00:00.000000

external_id is a caller-chosen correlation label, not an identity — a caller may reuse it
(e.g. re-running OCR on the same document). The unique per-job identity and storage key is
jobs.id (the server PK), so the (username, external_id) uniqueness was wrong and blocked
legitimate re-submissions.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "011"
down_revision: str | None = "010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_jobs_username_external", "jobs", type_="unique")


def downgrade() -> None:
    op.create_unique_constraint(
        "uq_jobs_username_external", "jobs", ["username", "external_id"]
    )
