from datetime import date, datetime

from sqlalchemy import func, text
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base


class JobDailyStats(Base):
    """Permanent daily rollup of jobs, written by the cleanup worker just before the
    raw rows are deleted (30-day retention). One row per
    (day, username, engine_version, domain). Counts plus the processing-time
    distribution; kept forever."""

    __tablename__ = "job_daily_stats"

    stat_date: Mapped[date] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(primary_key=True)
    engine_version: Mapped[str] = mapped_column(
        primary_key=True, server_default=text("'unknown'")
    )
    domain: Mapped[str] = mapped_column(primary_key=True, server_default=text("'default'"))

    jobs_total: Mapped[int] = mapped_column(nullable=False)
    jobs_done: Mapped[int] = mapped_column(nullable=False)
    jobs_failed: Mapped[int] = mapped_column(nullable=False)
    requeues_total: Mapped[int] = mapped_column(nullable=False)

    proc_count: Mapped[int] = mapped_column(nullable=False)
    proc_avg_seconds: Mapped[float | None] = mapped_column(default=None)
    proc_stddev_seconds: Mapped[float | None] = mapped_column(default=None)
    proc_min_seconds: Mapped[float | None] = mapped_column(default=None)
    proc_max_seconds: Mapped[float | None] = mapped_column(default=None)
    proc_p50_seconds: Mapped[float | None] = mapped_column(default=None)
    proc_p95_seconds: Mapped[float | None] = mapped_column(default=None)
    proc_p99_seconds: Mapped[float | None] = mapped_column(default=None)

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
