"""Analytics fact table, domain routing, priority queues, per-job metrics

Revision ID: 007
Revises: 006
Create Date: 2026-06-23 00:00:00.000000
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "007"
down_revision: str | None = "006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Drop the old rollup table — job_analytics replaces it permanently.
    op.drop_table("job_daily_stats")

    # --- Lookup tables (insert on first use: INSERT ... ON CONFLICT DO NOTHING) ---
    op.create_table(
        "engine_versions",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text, unique=True, nullable=False),
    )

    op.create_table(
        "domains",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text, unique=True, nullable=False),
    )

    # --- Domain → backend mapping (populated from GET /api/v1/models on healthcheck) ---
    op.create_table(
        "backend_domains",
        sa.Column("backend_id", sa.Integer, sa.ForeignKey("backends.id", ondelete="CASCADE"), nullable=False),
        sa.Column("domain_id", sa.Integer, sa.ForeignKey("domains.id", ondelete="CASCADE"), nullable=False),
        sa.PrimaryKeyConstraint("backend_id", "domain_id"),
    )

    # --- Enum types ---
    # Created explicitly here and referenced below with postgresql.ENUM(create_type=
    # False) so create_table does NOT try to emit CREATE TYPE again. (The generic
    # sa.Enum has no create_type kwarg — it silently auto-creates the type on table
    # create, which would collide with these statements.)
    op.execute("CREATE TYPE job_status_t    AS ENUM ('done', 'failed')")
    op.execute("CREATE TYPE engine_device_t AS ENUM ('gpu', 'cpu')")

    # --- Permanent analytics fact table (never purged) ---
    op.create_table(
        "job_analytics",
        sa.Column("job_id", sa.UUID, primary_key=True),
        sa.Column("external_id", sa.UUID, nullable=True),
        sa.Column("submitted_at", sa.DateTime, nullable=False),
        sa.Column("stat_date", sa.Date, nullable=False),
        sa.Column("user_id", sa.Integer, sa.ForeignKey("users.id"), nullable=True),
        sa.Column("engine_version_id", sa.Integer, sa.ForeignKey("engine_versions.id"), nullable=True),
        sa.Column("engine_device", postgresql.ENUM("gpu", "cpu", name="engine_device_t", create_type=False), nullable=True),
        sa.Column("backend_id", sa.Integer, sa.ForeignKey("backends.id"), nullable=True),
        sa.Column("domain_id", sa.Integer, sa.ForeignKey("domains.id"), nullable=True),
        sa.Column("fmt", sa.Text, nullable=True),
        sa.Column("status", postgresql.ENUM("done", "failed", name="job_status_t", create_type=False), nullable=False),
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
    op.create_index("ix_job_analytics_device_stat", "job_analytics", ["engine_device", "stat_date"])
    op.create_index("ix_job_analytics_ev_user", "job_analytics", ["engine_version_id", "user_id"])

    # --- Additions to existing tables ---
    op.add_column("jobs", sa.Column("file_size_bytes", sa.BigInteger, nullable=True))

    op.add_column("users", sa.Column("external_url_template", sa.Text, nullable=True))
    op.add_column("users", sa.Column("priority", sa.Integer, nullable=False, server_default="0"))

    op.add_column("backends", sa.Column("priority", sa.Integer, nullable=False, server_default="0"))
    op.add_column(
        "backends",
        sa.Column(
            "device",
            sa.Text,
            nullable=False,
            server_default="cpu",
        ),
    )
    op.create_check_constraint("ck_backends_device", "backends", "device IN ('gpu', 'cpu')")


def downgrade() -> None:
    op.drop_constraint("ck_backends_device", "backends", type_="check")
    op.drop_column("backends", "device")
    op.drop_column("backends", "priority")
    op.drop_column("users", "priority")
    op.drop_column("users", "external_url_template")
    op.drop_column("jobs", "file_size_bytes")

    op.drop_index("ix_job_analytics_ev_user", "job_analytics")
    op.drop_index("ix_job_analytics_device_stat", "job_analytics")
    op.drop_index("ix_job_analytics_user_stat", "job_analytics")
    op.drop_index("ix_job_analytics_stat_date", "job_analytics")
    op.drop_table("job_analytics")

    op.execute("DROP TYPE job_status_t")
    op.execute("DROP TYPE engine_device_t")

    op.drop_table("backend_domains")
    op.drop_table("domains")
    op.drop_table("engine_versions")

    # Recreate job_daily_stats (schema only — data cannot be recovered)
    op.create_table(
        "job_daily_stats",
        sa.Column("stat_date", sa.Date, primary_key=True),
        sa.Column("username", sa.String, primary_key=True),
        sa.Column("engine_version", sa.String, primary_key=True, server_default=sa.text("'unknown'")),
        sa.Column("domain", sa.String, primary_key=True, server_default=sa.text("'default'")),
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
