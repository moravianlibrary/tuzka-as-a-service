"""Drop job_analytics and engine_versions (history lives in ClickHouse)

Revision ID: 018
Revises: 017
Create Date: 2026-10-21 00:00:00.000000

Step 5 moved job history to ClickHouse (taas.job_events) and backfilled it from
job_analytics. Run the backfill (app/ops/backfill_clickstack.py) BEFORE this migration:
it reads the table this drops. The downgrade recreates the tables empty.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "018"
down_revision: str | None = "017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("job_analytics")  # its indexes go with it
    op.execute("DROP TYPE job_status_t")
    op.execute("DROP TYPE engine_device_t")
    op.drop_table("engine_versions")


def downgrade() -> None:
    # Same DDL as 007, empty.
    op.create_table(
        "engine_versions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text, unique=True, nullable=False),
    )
    op.execute("CREATE TYPE job_status_t    AS ENUM ('done', 'failed')")
    op.execute("CREATE TYPE engine_device_t AS ENUM ('gpu', 'cpu')")
    op.create_table(
        "job_analytics",
        sa.Column("job_id", sa.UUID, primary_key=True),
        sa.Column("external_id", sa.UUID, nullable=True),
        sa.Column("submitted_at", sa.DateTime, nullable=False),
        sa.Column("stat_date", sa.Date, nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
        sa.Column(
            "engine_version_id", sa.Integer, sa.ForeignKey("engine_versions.id"), nullable=True
        ),
        sa.Column(
            "engine_device",
            postgresql.ENUM("gpu", "cpu", name="engine_device_t", create_type=False),
            nullable=True,
        ),
        sa.Column("backend_id", sa.Integer, sa.ForeignKey("backends.id"), nullable=True),
        sa.Column("domain_id", sa.Integer, sa.ForeignKey("domains.id"), nullable=True),
        sa.Column("fmt", sa.Text, nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM("done", "failed", name="job_status_t", create_type=False),
            nullable=False,
        ),
        sa.Column("file_size_bytes", sa.BigInteger, nullable=True),
        sa.Column("system_queue_s", sa.Float, nullable=True),
        sa.Column("engine_queue_s", sa.Float, nullable=True),
        sa.Column("ocr_running_s", sa.Float, nullable=True),
        sa.Column("time_in_system_s", sa.Float, nullable=True),
        sa.Column("alto_lines", sa.Integer, nullable=True),
        sa.Column("alto_blocks", sa.Integer, nullable=True),
        sa.Column("alto_chars", sa.Integer, nullable=True),
        sa.Column("mean_conf", sa.Float, nullable=True),
    )
    op.create_index("ix_job_analytics_stat_date", "job_analytics", ["stat_date"])
    op.create_index("ix_job_analytics_user_stat", "job_analytics", ["user_id", "stat_date"])
    op.create_index(
        "ix_job_analytics_device_stat", "job_analytics", ["engine_device", "stat_date"]
    )
    op.create_index("ix_job_analytics_ev_user", "job_analytics", ["engine_version_id", "user_id"])
