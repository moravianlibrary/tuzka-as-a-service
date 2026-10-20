"""Batches: the batches table, jobs.batch_id / jobs.source_name

Revision ID: 021
Revises: 020
Create Date: 2026-10-21 00:00:00.000000

A batch is one archive upload: one domain + one set of outputs + one options object,
with each page an ordinary job pointing at it. Deleting a batch deletes its jobs.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from alembic import op

revision: str = "021"
down_revision: str | None = "020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "batches",
        sa.Column("id", sa.UUID, primary_key=True),
        sa.Column("username", sa.Text, sa.ForeignKey("users.username"), nullable=False),
        sa.Column("external_id", sa.Text, nullable=True),
        sa.Column("domain", sa.Text, nullable=False),
        sa.Column("outputs", ARRAY(sa.Text), nullable=False),
        sa.Column("options", JSONB, nullable=False, server_default="{}"),
        sa.Column("status", sa.Text, nullable=False, server_default="open"),
        sa.Column("total", sa.Integer, nullable=False, server_default="0"),
        sa.Column("submitted_at", sa.DateTime, nullable=False, server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime, nullable=True),
        sa.Column("archive_built_at", sa.DateTime, nullable=True),
        # The built results archive; counts toward the results storage cap (step 6).
        sa.Column("archive_size_bytes", sa.BigInteger, nullable=True),
        sa.CheckConstraint("status IN ('open', 'queued', 'finished')", name="ck_batches_status"),
    )
    op.create_index("ix_batches_username_submitted", "batches", ["username", "submitted_at"])
    op.create_index("ix_batches_username_external_id", "batches", ["username", "external_id"])
    op.add_column(
        "jobs",
        sa.Column(
            "batch_id", sa.UUID, sa.ForeignKey("batches.id", ondelete="CASCADE"), nullable=True
        ),
    )
    op.add_column("jobs", sa.Column("source_name", sa.Text, nullable=True))
    op.create_index("ix_jobs_batch_status", "jobs", ["batch_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_jobs_batch_status", "jobs")
    op.drop_column("jobs", "source_name")
    op.drop_column("jobs", "batch_id")
    op.drop_index("ix_batches_username_external_id", "batches")
    op.drop_index("ix_batches_username_submitted", "batches")
    op.drop_table("batches")
