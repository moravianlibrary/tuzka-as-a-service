import asyncio
import csv
import io
from datetime import UTC, date, datetime, timedelta

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deps import get_redis, get_settings, require_master
from app.models.backend import Backend
from app.models.backend_domain import BackendDomain
from app.models.db import get_db
from app.models.domain import Domain
from app.models.job import Job
from app.models.user import User
from app.schemas.dashboard import DashboardBackend, DashboardStats, DashboardUser
from app.schemas.job import render_external_url
from app.services.engine_client import EngineClient
from app.services.redis_jobs import get_backend_inflight

router = APIRouter(dependencies=[Depends(require_master)])


def _naive_utc(dt: datetime | None) -> datetime | None:
    """Normalize a query-param datetime to naive UTC.

    The dashboard sends tz-aware ISO strings (``…Z``) but the analytics/jobs
    columns are ``TIMESTAMP WITHOUT TIME ZONE`` holding naive UTC. Converting
    here keeps range filters correct regardless of the Postgres session timezone."""
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(UTC).replace(tzinfo=None)


def _csv_cell(value):
    """Defuse CSV formula injection: a cell whose text starts with = + - @ is
    prefixed with a single quote so spreadsheet apps don't execute it as a formula."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@"):
        return "'" + value
    return value


@router.get(
    "/stats",
    response_model=DashboardStats,
    summary="Get aggregate stats",
    responses={401: {"description": "Missing or invalid master key"}},
)
async def get_stats(db: AsyncSession = Depends(get_db)):
    """Return aggregate job stats over the **last 24 hours**: total jobs, counts by
    status (by submission time), and two averages for done jobs — OCR running time
    (finished − started, engine clock) and total time in system (stored − submitted).
    Requires a master key."""
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

    # Average OCR running time of done jobs that finished in the window (engine clock).
    avg_ocr_result = await db.execute(
        select(
            func.avg(func.extract("epoch", Job.finished_at) - func.extract("epoch", Job.started_at))
        ).where(
            Job.status == "done",
            Job.finished_at >= cutoff,
            Job.started_at.is_not(None),
            Job.finished_at.is_not(None),
        )
    )
    avg_ocr_running = avg_ocr_result.scalar()

    # Average total time in system of done jobs stored in the window (submitted -> stored).
    avg_tis_result = await db.execute(
        select(
            func.avg(func.extract("epoch", Job.stored_at) - func.extract("epoch", Job.submitted_at))
        ).where(
            Job.status == "done",
            Job.stored_at >= cutoff,
            Job.stored_at.is_not(None),
            Job.submitted_at.is_not(None),
        )
    )
    avg_tis = avg_tis_result.scalar()

    return DashboardStats(
        total_jobs=total_jobs,
        jobs_by_status=jobs_by_status,
        avg_ocr_running_seconds=round(avg_ocr_running, 2) if avg_ocr_running else None,
        avg_time_in_system_seconds=round(avg_tis, 2) if avg_tis else None,
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
    query = (
        select(Job, Backend.label, Backend.url, User.external_url_template)
        .outerjoin(Backend, Job.backend_id == Backend.id)
        .outerjoin(User, Job.username == User.username)
    )
    count_query = select(func.count()).select_from(Job)

    from_date = _naive_utc(from_date)
    to_date = _naive_utc(to_date)

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
    rows = result.all()

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
                "engine_received_at": j.engine_received_at.isoformat() if j.engine_received_at else None,
                "started_at": j.started_at.isoformat() if j.started_at else None,
                "finished_at": j.finished_at.isoformat() if j.finished_at else None,
                "stored_at": j.stored_at.isoformat() if j.stored_at else None,
                "backend_id": j.backend_id,
                "backend": backend_label or backend_url,
                "engine_version": j.engine_version,
                "error": j.error,
                "external_url": render_external_url(url_template, j.external_id),
            }
            for j, backend_label, backend_url, url_template in rows
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

    referenced = await db.execute(
        select(Job.backend_id).where(Job.backend_id.is_not(None)).distinct()
    )
    backends_with_jobs = {row[0] for row in referenced.all()}

    # Domains served per backend (from the GET /api/v1/models sync), for the Domains column.
    domain_rows = await db.execute(
        select(BackendDomain.backend_id, Domain.name)
        .join(Domain, BackendDomain.domain_id == Domain.id)
        .order_by(Domain.name)
    )
    domains_by_backend: dict[int, list[str]] = {}
    for backend_id, domain_name in domain_rows.all():
        domains_by_backend.setdefault(backend_id, []).append(domain_name)

    engine_client = EngineClient()

    async def probe(b: Backend) -> DashboardBackend:
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
            priority=b.priority,
            device=b.device,
            managed=b.managed,
            healthy=healthy,
            domains=domains_by_backend.get(b.id, []),
            can_delete=b.id not in backends_with_jobs,
        )

    try:
        return await asyncio.gather(*(probe(b) for b in backends))
    finally:
        await engine_client.close()


# --- Analytics ---

_GRANULARITY_TRUNC = {"hour": "hour", "day": "day", "week": "week", "month": "month", "year": "year"}

# Max buckets per granularity so clients can't request unbounded result sets.
_MAX_BUCKETS = 500


def _bucket_count(from_dt: datetime, to_dt: datetime, granularity: str) -> int:
    delta = to_dt - from_dt
    seconds = max(delta.total_seconds(), 0)
    divisors = {"hour": 3600, "day": 86400, "week": 604800, "month": 2592000, "year": 31536000}
    return int(seconds / divisors.get(granularity, 86400)) + 1


@router.get(
    "/analytics/breakdown",
    summary="Analytics breakdown by time, engine, user, domain",
    responses={
        400: {"description": "Too many buckets — narrow the date range or use a coarser granularity"},
        401: {"description": "Missing or invalid master key"},
    },
)
async def analytics_breakdown(
    from_date: datetime = Query(...),
    to_date: datetime = Query(...),
    granularity: str = Query("day"),
    domain: str | None = Query(None),
    engine_device: str | None = Query(None),
    engine_version: str | None = Query(None),
    username: str | None = Query(None),
    page: int = Query(1, ge=1, le=10),
    db: AsyncSession = Depends(get_db),
):
    """Group job_analytics by time bucket × engine × device × user × domain.

    Returns up to 500 rows (50 per page, max 10 pages). Requires a master key."""
    if granularity not in _GRANULARITY_TRUNC:
        raise HTTPException(status_code=400, detail=f"granularity must be one of {list(_GRANULARITY_TRUNC)}")
    from_date = _naive_utc(from_date)
    to_date = _naive_utc(to_date)
    if _bucket_count(from_date, to_date, granularity) > _MAX_BUCKETS:
        raise HTTPException(
            status_code=400,
            detail=f"Too many {granularity} buckets in the requested range — narrow the window or use a coarser granularity",
        )

    offset = (page - 1) * 50
    result = await db.execute(
        text(
            "SELECT"
            "  DATE_TRUNC(:gran, ja.submitted_at) AS time_bucket,"
            "  u.username,"
            "  ev.name AS engine_version,"
            "  ja.engine_device,"
            "  d.name AS domain,"
            "  COUNT(*) AS jobs_total,"
            "  COUNT(*) FILTER (WHERE ja.status = 'done') AS jobs_done,"
            "  COUNT(*) FILTER (WHERE ja.status = 'failed') AS jobs_failed,"
            "  AVG(ja.ocr_running_s) AS proc_avg_s,"
            "  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY ja.ocr_running_s) AS proc_p95_s,"
            "  AVG(ja.alto_lines) AS avg_alto_lines,"
            "  AVG(ja.alto_chars) AS avg_alto_chars,"
            "  AVG(ja.mean_conf) AS avg_mean_conf"
            " FROM job_analytics ja"
            " LEFT JOIN users u ON u.id = ja.user_id"
            " LEFT JOIN engine_versions ev ON ev.id = ja.engine_version_id"
            " LEFT JOIN domains d ON d.id = ja.domain_id"
            " WHERE ja.submitted_at BETWEEN :from_date AND :to_date"
            "  AND (CAST(:username AS text) IS NULL OR u.username = :username)"
            "  AND (CAST(:domain AS text) IS NULL OR d.name = :domain)"
            "  AND (CAST(:engine_device AS text) IS NULL OR ja.engine_device::text = :engine_device)"
            "  AND (CAST(:engine_version AS text) IS NULL OR ev.name = :engine_version)"
            " GROUP BY time_bucket, u.username, ev.name, ja.engine_device, d.name"
            " ORDER BY time_bucket DESC"
            " LIMIT 51 OFFSET :offset"  # fetch 51 to detect next page
        ),
        {
            "gran": granularity,
            "from_date": from_date,
            "to_date": to_date,
            "username": username,
            "domain": domain,
            "engine_device": engine_device,
            "engine_version": engine_version,
            "offset": offset,
        },
    )
    rows = result.mappings().all()
    has_next = len(rows) > 50
    rows = rows[:50]

    def _round(v):
        return round(v, 3) if v is not None else None

    return {
        "page": page,
        "has_next": has_next,
        "rows": [
            {
                "time_bucket": r["time_bucket"].isoformat() if r["time_bucket"] else None,
                "username": r["username"],
                "engine_version": r["engine_version"],
                "engine_device": r["engine_device"],
                "domain": r["domain"],
                "jobs_total": r["jobs_total"],
                "jobs_done": r["jobs_done"],
                "jobs_failed": r["jobs_failed"],
                "proc_avg_s": _round(r["proc_avg_s"]),
                "proc_p95_s": _round(r["proc_p95_s"]),
                "avg_alto_lines": _round(r["avg_alto_lines"]),
                "avg_alto_chars": _round(r["avg_alto_chars"]),
                "avg_mean_conf": _round(r["avg_mean_conf"]),
            }
            for r in rows
        ],
    }


def _alto_range(category: str | None, column: str) -> str:
    """Return a SQL BETWEEN clause for an ALTO line/block/char category filter."""
    ranges: dict[str, dict[str, tuple[int, int | None]]] = {
        "alto_lines": {
            "empty": (0, 0), "sparse": (1, 15), "normal": (16, 60),
            "dense": (61, 300), "very_dense": (301, None),
        },
        "alto_blocks": {
            "empty": (0, 0), "simple": (1, 2), "multi": (3, 10),
            "complex": (11, 30), "fragmented": (31, None),
        },
        "alto_chars": {
            "empty": (0, 0), "sparse": (1, 499), "normal": (500, 3000), "rich": (3001, None),
        },
    }
    if category is None or column not in ranges or category not in ranges[column]:
        return ""
    lo, hi = ranges[column][category]
    if hi is None:
        return f" AND {column} >= {lo}"
    return f" AND {column} BETWEEN {lo} AND {hi}"


# Shared FROM/JOIN for the raw analytics queries (per-job rows + display names).
_ANALYTICS_FROM = (
    " FROM job_analytics ja"
    " LEFT JOIN users u ON u.id = ja.user_id"
    " LEFT JOIN engine_versions ev ON ev.id = ja.engine_version_id"
    " LEFT JOIN domains d ON d.id = ja.domain_id"
)


def _analytics_filters(
    *,
    from_date: datetime | None,
    to_date: datetime | None,
    username: str | None,
    domain: str | None,
    engine_device: str | None,
    engine_version: str | None,
    status: str | None,
    line_category: str | None,
    block_category: str | None,
    char_category: str | None,
) -> tuple[str, dict]:
    """Build the WHERE clause + bind params shared by /analytics/raw and
    /analytics/raw.csv, so the table view and the CSV export filter identically."""
    where = " WHERE 1=1"
    params: dict = {}
    from_date = _naive_utc(from_date)
    to_date = _naive_utc(to_date)
    if from_date:
        where += " AND ja.submitted_at >= :from_date"
        params["from_date"] = from_date
    if to_date:
        where += " AND ja.submitted_at <= :to_date"
        params["to_date"] = to_date
    if username:
        where += " AND u.username = :username"
        params["username"] = username
    if domain:
        where += " AND d.name = :domain"
        params["domain"] = domain
    if engine_device:
        where += " AND ja.engine_device::text = :engine_device"
        params["engine_device"] = engine_device
    if engine_version:
        where += " AND ev.name = :engine_version"
        params["engine_version"] = engine_version
    if status:
        where += " AND ja.status::text = :status"
        params["status"] = status
    where += _alto_range(line_category, "alto_lines")
    where += _alto_range(block_category, "alto_blocks")
    where += _alto_range(char_category, "alto_chars")
    return where, params


@router.get(
    "/analytics/raw",
    summary="Raw per-job analytics with filters",
    responses={
        400: {"description": "page > 10"},
        401: {"description": "Missing or invalid master key"},
    },
)
async def analytics_raw(
    from_date: datetime | None = Query(None),
    to_date: datetime | None = Query(None),
    username: str | None = Query(None),
    domain: str | None = Query(None),
    engine_device: str | None = Query(None),
    engine_version: str | None = Query(None),
    status: str | None = Query(None),
    line_category: str | None = Query(None),
    block_category: str | None = Query(None),
    char_category: str | None = Query(None),
    page: int = Query(1, ge=1, le=10),
    db: AsyncSession = Depends(get_db),
):
    """Return up to 50 raw job_analytics rows per page (max 10 pages / 500 rows total).

    Category filters map line/block/char counts to named buckets defined in the design doc.
    Requires a master key."""
    offset = (page - 1) * 50
    where, params = _analytics_filters(
        from_date=from_date, to_date=to_date, username=username, domain=domain,
        engine_device=engine_device, engine_version=engine_version, status=status,
        line_category=line_category, block_category=block_category,
        char_category=char_category,
    )

    result = await db.execute(
        text(
            "SELECT ja.job_id, ja.external_id, ja.submitted_at, ja.stat_date,"
            "  u.username, ja.engine_device, ja.backend_id,"
            "  ev.name AS engine_version, d.name AS domain,"
            "  ja.fmt, ja.status, ja.file_size_bytes,"
            "  ja.system_queue_s, ja.engine_queue_s, ja.ocr_running_s, ja.time_in_system_s,"
            "  ja.alto_lines, ja.alto_blocks, ja.alto_chars, ja.mean_conf"
            + _ANALYTICS_FROM
            + where
            + " ORDER BY ja.submitted_at DESC"
            " LIMIT 51 OFFSET :offset"  # fetch 51 to detect next page
        ),
        {**params, "offset": offset},
    )
    rows = result.mappings().all()
    has_next = len(rows) > 50
    rows = rows[:50]

    def _fmt_row(r):
        return {
            "job_id": str(r["job_id"]),
            "external_id": str(r["external_id"]) if r["external_id"] else None,
            "submitted_at": r["submitted_at"].isoformat() if r["submitted_at"] else None,
            "username": r["username"],
            "engine_version": r["engine_version"],
            "engine_device": r["engine_device"],
            "domain": r["domain"],
            "fmt": r["fmt"],
            "status": r["status"],
            "file_size_bytes": r["file_size_bytes"],
            "system_queue_s": r["system_queue_s"],
            "engine_queue_s": r["engine_queue_s"],
            "ocr_running_s": r["ocr_running_s"],
            "time_in_system_s": r["time_in_system_s"],
            "alto_lines": r["alto_lines"],
            "alto_blocks": r["alto_blocks"],
            "alto_chars": r["alto_chars"],
            "mean_conf": r["mean_conf"],
        }

    return {"page": page, "has_next": has_next, "rows": [_fmt_row(r) for r in rows]}


@router.get(
    "/analytics/raw.csv",
    summary="Export raw analytics as CSV (admin only, no page limit)",
    responses={401: {"description": "Missing or invalid master key"}},
)
async def analytics_raw_csv(
    from_date: datetime | None = Query(None),
    to_date: datetime | None = Query(None),
    username: str | None = Query(None),
    domain: str | None = Query(None),
    engine_device: str | None = Query(None),
    engine_version: str | None = Query(None),
    status: str | None = Query(None),
    line_category: str | None = Query(None),
    block_category: str | None = Query(None),
    char_category: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
):
    """Stream the full filtered result set from job_analytics as a CSV download.

    No page limit — use date filters to bound the result. Requires a master key."""
    where, params = _analytics_filters(
        from_date=from_date, to_date=to_date, username=username, domain=domain,
        engine_device=engine_device, engine_version=engine_version, status=status,
        line_category=line_category, block_category=block_category,
        char_category=char_category,
    )

    result = await db.execute(
        text(
            "SELECT ja.submitted_at, ja.job_id, ja.external_id,"
            "  u.username, ja.status::text, ja.fmt,"
            "  d.name AS domain, ev.name AS engine_version, ja.engine_device::text,"
            "  ja.file_size_bytes,"
            "  ja.system_queue_s, ja.engine_queue_s, ja.ocr_running_s, ja.time_in_system_s,"
            "  ja.alto_lines, ja.alto_blocks, ja.alto_chars, ja.mean_conf"
            + _ANALYTICS_FROM
            + where
            + " ORDER BY ja.submitted_at DESC"
        ),
        params,
    )
    rows = result.all()

    CSV_COLUMNS = [
        "submitted_at", "job_id", "external_id", "username", "status", "fmt",
        "domain", "engine_version", "engine_device", "file_size_bytes",
        "system_queue_s", "engine_queue_s", "ocr_running_s", "time_in_system_s",
        "alto_lines", "alto_blocks", "alto_chars", "mean_conf",
    ]
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(CSV_COLUMNS)
    for row in rows:
        writer.writerow([_csv_cell(v) for v in row])
    buf.seek(0)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="taas-analytics.csv"'},
    )


@router.get(
    "/stats/years",
    summary="Years that have analytics data",
    responses={401: {"description": "Missing or invalid master key"}},
)
async def stats_years(db: AsyncSession = Depends(get_db)):
    """Return the calendar years that have data in job_analytics, newest first.

    Falls back to the current year when there is no data yet. Requires a master key."""
    result = await db.execute(
        text(
            "SELECT DISTINCT EXTRACT(YEAR FROM stat_date)::int AS yr"
            " FROM job_analytics"
            " ORDER BY yr DESC"
        )
    )
    years = [row[0] for row in result.all()]
    return {"years": years or [datetime.utcnow().year]}


@router.get(
    "/stats.csv",
    summary="Download aggregated usage stats as CSV",
    responses={401: {"description": "Missing or invalid master key"}},
)
async def download_stats_csv(
    year: int | None = Query(None, description="Calendar year; defaults to current year"),
    db: AsyncSession = Depends(get_db),
):
    """Stream a CSV of daily usage stats for ``year`` (default: current year).

    Aggregates job_analytics fact rows into daily summaries grouped by
    username × engine_version × domain. Requires a master key."""
    year = year or datetime.utcnow().year
    start = date(year, 1, 1)
    end = date(year + 1, 1, 1)

    STATS_COLUMNS = (
        "stat_date", "username", "engine_version", "domain",
        "jobs_total", "jobs_done", "jobs_failed",
        "proc_count", "proc_avg_seconds", "proc_stddev_seconds",
        "proc_min_seconds", "proc_max_seconds",
        "proc_p50_seconds", "proc_p95_seconds", "proc_p99_seconds",
    )

    result = await db.execute(
        text(
            "SELECT"
            "  ja.stat_date,"
            "  COALESCE(u.username, 'unknown') AS username,"
            "  COALESCE(ev.name, 'unknown') AS engine_version,"
            "  COALESCE(d.name, 'default') AS domain,"
            "  COUNT(*) AS jobs_total,"
            "  COUNT(*) FILTER (WHERE ja.status = 'done') AS jobs_done,"
            "  COUNT(*) FILTER (WHERE ja.status = 'failed') AS jobs_failed,"
            "  COUNT(ja.ocr_running_s) AS proc_count,"
            "  AVG(ja.ocr_running_s) AS proc_avg_seconds,"
            "  STDDEV_POP(ja.ocr_running_s) AS proc_stddev_seconds,"
            "  MIN(ja.ocr_running_s) AS proc_min_seconds,"
            "  MAX(ja.ocr_running_s) AS proc_max_seconds,"
            "  PERCENTILE_CONT(0.5)  WITHIN GROUP (ORDER BY ja.ocr_running_s) AS proc_p50_seconds,"
            "  PERCENTILE_CONT(0.95) WITHIN GROUP (ORDER BY ja.ocr_running_s) AS proc_p95_seconds,"
            "  PERCENTILE_CONT(0.99) WITHIN GROUP (ORDER BY ja.ocr_running_s) AS proc_p99_seconds"
            " FROM job_analytics ja"
            " LEFT JOIN users u ON u.id = ja.user_id"
            " LEFT JOIN engine_versions ev ON ev.id = ja.engine_version_id"
            " LEFT JOIN domains d ON d.id = ja.domain_id"
            " WHERE ja.stat_date >= :start AND ja.stat_date < :end"
            " GROUP BY ja.stat_date, u.username, ev.name, d.name"
            " ORDER BY ja.stat_date"
        ),
        {"start": start, "end": end},
    )
    rows = result.all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(STATS_COLUMNS)
    for row in rows:
        writer.writerow([_csv_cell(v) for v in row])
    buf.seek(0)

    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="taas-stats-{year}.csv"'},
    )
