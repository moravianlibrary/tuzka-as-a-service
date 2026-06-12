"""Add jobs.engine_version, job_daily_stats rollup table; drop dead retention config

Revision ID: 005
Revises: 004
Create Date: 2026-06-12 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "005"
down_revision: str | None = "004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("jobs", sa.Column("engine_version", sa.String, nullable=True))

    op.create_table(
        "job_daily_stats",
        sa.Column("stat_date", sa.Date, primary_key=True),
        sa.Column("username", sa.String, primary_key=True),
        sa.Column(
            "engine_version",
            sa.String,
            primary_key=True,
            server_default=sa.text("'unknown'"),
        ),
        sa.Column(
            "domain",
            sa.String,
            primary_key=True,
            server_default=sa.text("'default'"),
        ),
        sa.Column("jobs_total", sa.Integer, nullable=False),
        sa.Column("jobs_done", sa.Integer, nullable=False),
        sa.Column("jobs_failed", sa.Integer, nullable=False),
        sa.Column("requeues_total", sa.Integer, nullable=False),
        sa.Column("proc_count", sa.Integer, nullable=False),
        sa.Column("proc_avg_seconds", sa.Float, nullable=True),
        sa.Column("proc_stddev_seconds", sa.Float, nullable=True),
        sa.Column("proc_min_seconds", sa.Float, nullable=True),
        sa.Column("proc_max_seconds", sa.Float, nullable=True),
        sa.Column("proc_p50_seconds", sa.Float, nullable=True),
        sa.Column("proc_p95_seconds", sa.Float, nullable=True),
        sa.Column("proc_p99_seconds", sa.Float, nullable=True),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("now()")),
    )

    # Retention is now hardcoded (30 days) in the cleanup worker; drop the stale key.
    op.execute("DELETE FROM config WHERE key = 'jobs.retention_days'")


def downgrade() -> None:
    op.drop_table("job_daily_stats")
    op.drop_column("jobs", "engine_version")
