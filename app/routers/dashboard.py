import asyncio
import csv
import io
from datetime import date, datetime, timedelta

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deps import get_redis, get_settings, require_master
from app.models.backend import Backend
from app.models.db import get_db
from app.models.job import Job
from app.schemas.dashboard import DashboardBackend, DashboardStats, DashboardUser
from app.services.engine_client import EngineClient
from app.services.redis_jobs import get_backend_inflight
from app.services.stats import STATS_COLUMNS, daily_aggregation_select

router = APIRouter(dependencies=[Depends(require_master)])


@router.get(
    "/stats",
    response_model=DashboardStats,
    summary="Get aggregate stats",
    responses={401: {"description": "Missing or invalid master key"}},
)
async def get_stats(db: AsyncSession = Depends(get_db)):
    """Return aggregate job stats over the **last 24 hours**: total jobs, counts by
    status (both by submission time), and average done-job duration (by completion
    time). Requires a master key."""
    cutoff = datetime.utcnow() - timedelta(hours=24)

    # Total jobs submitted in the window.
    total = await db.execute(
        select(func.count()).select_from(Job).where(Job.submitted_at >= cutoff)
    )
    total_jobs = total.scalar() or 0

    # Jobs by status in the window.
    by_status_result = await db.execute(
        select(Job.status, func.count())
        .where(Job.submitted_at >= cutoff)
        .group_by(Job.status)
    )
    jobs_by_status = {row[0]: row[1] for row in by_status_result.all()}

    # Average duration of done jobs that finished in the window.
    avg_result = await db.execute(
        select(
            func.avg(func.extract("epoch", Job.finished_at) - func.extract("epoch", Job.started_at))
        ).where(
            Job.status == "done",
            Job.finished_at >= cutoff,
            Job.started_at.is_not(None),
            Job.finished_at.is_not(None),
        )
    )
    avg_duration = avg_result.scalar()

    return DashboardStats(
        total_jobs=total_jobs,
        jobs_by_status=jobs_by_status,
        avg_duration_seconds=round(avg_duration, 2) if avg_duration else None,
    )


@router.get(
    "/users",
    response_model=list[DashboardUser],
    summary="Get per-user job stats",
    responses={401: {"description": "Missing or invalid master key"}},
)
async def get_dashboard_users(db: AsyncSession = Depends(get_db)):
    """Return per-user job stats grouped by username: total jobs, done and failed
    counts, and last-active timestamp. Requires a master key."""
    result = await db.execute(
        select(
            Job.username,
            func.count().label("total_jobs"),
            func.sum(case((Job.status == "done", 1), else_=0)).label("done"),
            func.sum(case((Job.status == "failed", 1), else_=0)).label("failed"),
            func.max(Job.submitted_at).label("last_active"),
        ).group_by(Job.username)
    )
    # Every row here has jobs by construction, so none are deletable; users with no
    # jobs simply don't appear in this query (the frontend treats their absence as
    # deletable).
    return [
        DashboardUser(
            username=row.username,
            total_jobs=row.total_jobs,
            done=row.done or 0,
            failed=row.failed or 0,
            last_active=row.last_active,
            can_delete=row.total_jobs == 0,
        )
        for row in result.all()
    ]


@router.get(
    "/jobs",
    summary="List jobs (admin)",
    responses={401: {"description": "Missing or invalid master key"}},
)
async def get_dashboard_jobs(
    username: str | None = Query(None),
    status: str | None = Query(None),
    from_date: datetime | None = Query(None, alias="from"),
    to_date: datetime | None = Query(None, alias="to"),
    limit: int = Query(50),
    offset: int = Query(0),
    db: AsyncSession = Depends(get_db),
):
    """List jobs newest-first with optional username/status/from/to date filters and
    limit/offset pagination, returning the matching jobs plus the total filtered count.
    Requires a master key."""
    query = select(Job)
    count_query = select(func.count()).select_from(Job)

    if username:
        query = query.where(Job.username == username)
        count_query = count_query.where(Job.username == username)
    if status:
        query = query.where(Job.status == status)
        count_query = count_query.where(Job.status == status)
    if from_date:
        query = query.where(Job.submitted_at >= from_date)
        count_query = count_query.where(Job.submitted_at >= from_date)
    if to_date:
        query = query.where(Job.submitted_at <= to_date)
        count_query = count_query.where(Job.submitted_at <= to_date)

    query = query.order_by(Job.submitted_at.desc()).limit(limit).offset(offset)

    result = await db.execute(query)
    jobs = result.scalars().all()

    total_result = await db.execute(count_query)
    total = total_result.scalar() or 0

    return {
        "jobs": [
            {
                "job_id": str(j.id),
                "username": j.username,
                "external_id": str(j.external_id),
                "status": j.status,
                "fmt": j.fmt,
                "domain": j.domain,
                "submitted_at": j.submitted_at.isoformat() if j.submitted_at else None,
                "dispatched_at": j.dispatched_at.isoformat() if j.dispatched_at else None,
                "started_at": j.started_at.isoformat() if j.started_at else None,
                "finished_at": j.finished_at.isoformat() if j.finished_at else None,
                "stored_at": j.stored_at.isoformat() if j.stored_at else None,
                "error": j.error,
            }
            for j in jobs
        ],
        "total": total,
    }


@router.get(
    "/usage",
    summary="Daily usage by user and status",
    responses={401: {"description": "Missing or invalid master key"}},
)
async def get_usage(
    days: int = Query(30, ge=1, le=90),
    db: AsyncSession = Depends(get_db),
):
    """Return daily job counts over the trailing ``days`` window (1-90). Provides both a
    per-user ``series`` and a per-status ``status_series`` (done/failed/queued/running)
    aligned to the same ``days`` axis. Requires a master key."""
    start = (datetime.utcnow() - timedelta(days=days - 1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    day_col = func.date(Job.submitted_at)
    result = await db.execute(
        select(day_col.label("day"), Job.username, func.count().label("c"))
        .where(Job.submitted_at >= start)
        .group_by(day_col, Job.username)
    )
    rows = result.all()

    # Same window, grouped by status instead of user, for the status chart.
    status_result = await db.execute(
        select(day_col.label("day"), Job.status, func.count().label("c"))
        .where(Job.submitted_at >= start)
        .group_by(day_col, Job.status)
    )
    status_rows = status_result.all()

    day_list = [(start + timedelta(days=i)).date().isoformat() for i in range(days)]
    day_index = {d: i for i, d in enumerate(day_list)}

    def day_of(row):
        return row.day.isoformat() if hasattr(row.day, "isoformat") else str(row.day)

    users = sorted({row.username for row in rows})
    series = {u: [0] * days for u in users}
    for row in rows:
        if (i := day_index.get(day_of(row))) is not None:
            series[row.username][i] = row.c

    # Fixed status set/order so the chart's colours and legend stay stable.
    statuses = ["done", "failed", "queued", "running"]
    status_series = {s: [0] * days for s in statuses}
    for row in status_rows:
        if row.status in status_series and (i := day_index.get(day_of(row))) is not None:
            status_series[row.status][i] = row.c

    return {
        "days": day_list,
        "users": users,
        "series": series,
        "statuses": statuses,
        "status_series": status_series,
    }


@router.get(
    "/backends",
    response_model=list[DashboardBackend],
    summary="List backends with live health",
    responses={401: {"description": "Missing or invalid master key"}},
)
async def get_dashboard_backends(
    db: AsyncSession = Depends(get_db),
    r: aioredis.Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
):
    """List configured backends with their config plus live state, probing every backend
    concurrently for current in-flight count (Redis) and health (engine healthcheck).
    Disabled backends are not health-probed (``healthy`` is reported as ``null``).
    Requires a master key."""
    result = await db.execute(select(Backend).order_by(Backend.id))
    backends = result.scalars().all()

    # Backend ids still referenced by jobs (one query) — those can't be hard-deleted.
    referenced = await db.execute(
        select(Job.backend_id).where(Job.backend_id.is_not(None)).distinct()
    )
    backends_with_jobs = {row[0] for row in referenced.all()}

    engine_client = EngineClient()

    async def probe(b: Backend) -> DashboardBackend:
        # Don't health-probe a disabled backend: it's intentionally out of
        # rotation, so hitting it would be wasted load and misleading noise.
        if b.enabled:
            inflight, healthy = await asyncio.gather(
                get_backend_inflight(r, b.id),
                engine_client.healthcheck(b.url),
            )
        else:
            inflight, healthy = await get_backend_inflight(r, b.id), None
        return DashboardBackend(
            id=b.id,
            url=b.url,
            label=b.label,
            enabled=b.enabled,
            max_inflight=b.max_inflight,
            inflight_now=inflight,
            healthy=healthy,
            can_delete=b.id not in backends_with_jobs,
        )

    try:
        # Probe every backend concurrently so one slow/unreachable host can't
        # serialise the whole response (worst case ~= one healthcheck timeout).
        return await asyncio.gather(*(probe(b) for b in backends))
    finally:
        await engine_client.close()


@router.get(
    "/stats/years",
    summary="Years that have usage data",
    responses={401: {"description": "Missing or invalid master key"}},
)
async def stats_years(db: AsyncSession = Depends(get_db)):
    """Return the calendar years that actually have data — from the permanent
    ``job_daily_stats`` rollup and the live ``jobs`` tail — newest first, so the
    export year picker only offers real years. Falls back to the current year when
    there is no data yet. Requires a master key."""
    result = await db.execute(
        text(
            "SELECT DISTINCT yr FROM ("
            "  SELECT EXTRACT(YEAR FROM stat_date)::int AS yr FROM job_daily_stats"
            "  UNION"
            "  SELECT EXTRACT(YEAR FROM finished_at)::int AS yr FROM jobs"
            "  WHERE finished_at IS NOT NULL"
            ") y ORDER BY yr DESC"
        )
    )
    years = [row[0] for row in result.all()]
    return {"years": years or [datetime.utcnow().year]}


@router.get(
    "/stats.csv",
    summary="Download usage stats as CSV",
    responses={401: {"description": "Missing or invalid master key"}},
)
async def download_stats_csv(
    year: int | None = Query(None, description="Calendar year; defaults to current year"),
    db: AsyncSession = Depends(get_db),
):
    """Stream a CSV of daily usage stats for ``year`` (default: current year).

    Unions the permanent ``job_daily_stats`` rollup (days already aged out of the raw
    ``jobs`` table) with the same aggregation computed live over the recent tail still
    in ``jobs``. The two sources never overlap — a day is either rolled-up-and-deleted
    or still live — so the full year is covered exactly once. Requires a master key."""
    year = year or datetime.utcnow().year
    start = date(year, 1, 1)
    end = date(year + 1, 1, 1)

    cols = ", ".join(STATS_COLUMNS)
    recent = daily_aggregation_select("finished_at >= :start AND finished_at < :end")
    query = text(
        f"SELECT {cols} FROM job_daily_stats "
        "WHERE stat_date >= :start AND stat_date < :end\n"
        "UNION ALL\n"
        f"{recent}\n"
        "ORDER BY stat_date"
    )
    result = await db.execute(query, {"start": start, "end": end})
    rows = result.all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(STATS_COLUMNS)
    writer.writerows(rows)
    buf.seek(0)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="taas-stats-{year}.csv"'},
    )
