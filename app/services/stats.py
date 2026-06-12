"""Shared daily aggregation over the ``jobs`` table.

Used by both the retention rollup (cleanup worker, which aggregates aged-out days
into ``job_daily_stats`` before deleting them) and the CSV export (which aggregates
the still-live tail of recent jobs). Keeping one definition guarantees historical
rolled-up rows and freshly-aggregated rows carry identical metrics, so they line up
in a single report.

The percentile/avg/stddev aggregates all ignore NULL durations automatically, so a
job without both ``started_at`` and ``finished_at`` is counted in the totals but
excluded from the timing distribution.
"""

# Output column order — matches the job_daily_stats table and the INSERT below.
STATS_COLUMNS = (
    "stat_date",
    "username",
    "engine_version",
    "domain",
    "jobs_total",
    "jobs_done",
    "jobs_failed",
    "requeues_total",
    "proc_count",
    "proc_avg_seconds",
    "proc_stddev_seconds",
    "proc_min_seconds",
    "proc_max_seconds",
    "proc_p50_seconds",
    "proc_p95_seconds",
    "proc_p99_seconds",
)


def daily_aggregation_select(where_clause: str) -> str:
    """Return the day × username × engine_version × domain aggregation SELECT over
    ``jobs``, restricted by ``where_clause`` (raw SQL, may reference bind params).
    Columns are emitted in ``STATS_COLUMNS`` order."""
    return f"""
        SELECT
            finished_at::date AS stat_date,
            username,
            COALESCE(engine_version, 'unknown') AS engine_version,
            COALESCE(domain, 'default') AS domain,
            count(*) AS jobs_total,
            count(*) FILTER (WHERE status = 'done') AS jobs_done,
            count(*) FILTER (WHERE status = 'failed') AS jobs_failed,
            COALESCE(sum(requeues), 0) AS requeues_total,
            count(d) AS proc_count,
            avg(d) AS proc_avg_seconds,
            stddev_pop(d) AS proc_stddev_seconds,
            min(d) AS proc_min_seconds,
            max(d) AS proc_max_seconds,
            percentile_cont(0.5)  WITHIN GROUP (ORDER BY d) AS proc_p50_seconds,
            percentile_cont(0.95) WITHIN GROUP (ORDER BY d) AS proc_p95_seconds,
            percentile_cont(0.99) WITHIN GROUP (ORDER BY d) AS proc_p99_seconds
        FROM (
            SELECT
                username, status, requeues, engine_version, domain, finished_at,
                EXTRACT(EPOCH FROM (finished_at - started_at)) AS d
            FROM jobs
            WHERE {where_clause}
        ) j
        GROUP BY
            finished_at::date, username,
            COALESCE(engine_version, 'unknown'), COALESCE(domain, 'default')
    """
