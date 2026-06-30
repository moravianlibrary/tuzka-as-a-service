"""Integration tests for job_analytics writes and the analytics endpoints.

Needs a real Postgres (enums, ON CONFLICT, DATE_TRUNC, PERCENTILE_CONT are
Postgres-specific) pointed at a throwaway, already-migrated database via
``TEST_DATABASE_URL``. Skips when that isn't set or isn't reachable — same spirit as
the Redis-backed and rollup tests.

The endpoint coroutines are called directly with a session rather than over HTTP, so
the master-key dependency is bypassed. Because their parameter defaults are FastAPI
``Query(...)`` objects (not plain values), every call passes all parameters explicitly.
"""

import os
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.routers.dashboard import analytics_breakdown, analytics_raw, analytics_raw_csv
from app.services.analytics import write_analytics_row

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

BASE = datetime(2026, 6, 1, 12, 0, 0)


@pytest.fixture
async def session():
    if not TEST_DATABASE_URL:
        pytest.skip("Set TEST_DATABASE_URL to a throwaway migrated DB to run analytics tests")
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("TEST_DATABASE_URL not reachable")

    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as s:
        await s.execute(
            text(
                "TRUNCATE job_analytics, backend_domains, domains, engine_versions, "
                "job_results, jobs, backends, users RESTART IDENTITY CASCADE"
            )
        )
        await s.execute(
            text("INSERT INTO users (username, hashed_key) VALUES ('alice', 'x'), ('bob', 'x')")
        )
        await s.execute(text("INSERT INTO backends (id, url) VALUES (1, 'http://e')"))
        await s.commit()
        yield s
    await engine.dispose()


async def _write(
    session,
    *,
    username="alice",
    status="done",
    engine_version="1.4.0",
    engine_device="gpu",
    domain="mzk",
    fmt="multi",
    submitted=BASE,
    dispatched=None,
    engine_received=None,
    started=None,
    finished=None,
    stored=None,
    alto_lines=None,
    alto_blocks=None,
    alto_chars=None,
    mean_conf=None,
    file_size_bytes=1000,
    job_id=None,
):
    await write_analytics_row(
        session,
        job_id=job_id or uuid.uuid4(),
        external_id=uuid.uuid4(),
        submitted_at=submitted,
        username=username,
        engine_version=engine_version,
        engine_device=engine_device,
        backend_id=1,
        domain=domain,
        fmt=fmt,
        status=status,
        file_size_bytes=file_size_bytes,
        dispatched_at=dispatched,
        engine_received_at=engine_received,
        started_at=started,
        finished_at=finished,
        stored_at=stored,
        alto_lines=alto_lines,
        alto_blocks=alto_blocks,
        alto_chars=alto_chars,
        mean_conf=mean_conf,
    )
    await session.commit()


# --- write_analytics_row ---


@pytest.mark.asyncio
async def test_write_done_job_populates_row_durations_and_lookups(session):
    await _write(
        session,
        dispatched=BASE + timedelta(seconds=2),  # taas clock: submit POSTs
        engine_received=BASE + timedelta(seconds=3),  # engine clock: job queued on engine
        started=BASE + timedelta(seconds=5),
        finished=BASE + timedelta(seconds=15),
        stored=BASE + timedelta(seconds=18),
        alto_lines=35,
        alto_blocks=5,
        alto_chars=1390,
        mean_conf=0.91,
    )

    row = (
        await session.execute(
            text(
                "SELECT ja.status::text, ja.stat_date, ja.system_queue_s, ja.engine_queue_s,"
                "  ja.ocr_running_s, ja.time_in_system_s, ja.alto_lines, ja.mean_conf,"
                "  u.username, ev.name AS engine_version, d.name AS domain, ja.engine_device::text"
                " FROM job_analytics ja"
                " LEFT JOIN users u ON u.id = ja.user_id"
                " LEFT JOIN engine_versions ev ON ev.id = ja.engine_version_id"
                " LEFT JOIN domains d ON d.id = ja.domain_id"
            )
        )
    ).mappings().one()

    assert row["status"] == "done"
    assert row["stat_date"] == BASE.date()
    assert row["system_queue_s"] == pytest.approx(2.0)  # submitted -> dispatched (taas)
    assert row["engine_queue_s"] == pytest.approx(2.0)  # engine_received -> started (engine)
    assert row["ocr_running_s"] == pytest.approx(10.0)
    assert row["time_in_system_s"] == pytest.approx(18.0)
    assert row["alto_lines"] == 35
    assert row["mean_conf"] == pytest.approx(0.91)
    # FK lookups resolved + lookup tables populated on first use.
    assert row["username"] == "alice"
    assert row["engine_version"] == "1.4.0"
    assert row["domain"] == "mzk"
    assert row["engine_device"] == "gpu"


@pytest.mark.asyncio
async def test_write_failed_before_dispatch_leaves_timings_null(session):
    # Job failed before it was ever dispatched: only submitted_at is known.
    await _write(
        session,
        status="failed",
        engine_version=None,
        engine_device=None,
        dispatched=None,
        started=None,
        finished=None,
        stored=None,
    )

    row = (
        await session.execute(
            text(
                "SELECT status::text, system_queue_s, engine_queue_s, ocr_running_s,"
                "  time_in_system_s, engine_device FROM job_analytics"
            )
        )
    ).mappings().one()
    assert row["status"] == "failed"
    assert row["system_queue_s"] is None
    assert row["engine_queue_s"] is None
    assert row["ocr_running_s"] is None
    assert row["time_in_system_s"] is None
    assert row["engine_device"] is None


@pytest.mark.asyncio
async def test_write_is_idempotent_on_job_id_conflict(session):
    jid = uuid.uuid4()
    await _write(session, job_id=jid, mean_conf=0.5)
    await _write(session, job_id=jid, mean_conf=0.9)  # ON CONFLICT DO NOTHING

    count = (await session.execute(text("SELECT count(*) FROM job_analytics"))).scalar()
    assert count == 1
    mc = (await session.execute(text("SELECT mean_conf FROM job_analytics"))).scalar()
    assert mc == pytest.approx(0.5)  # first write wins


# --- /analytics/raw ---


async def _raw(session, **overrides):
    params = dict(
        from_date=None, to_date=None, username=None, domain=None, engine_device=None,
        engine_version=None, status=None, line_category=None, block_category=None,
        char_category=None, page=1,
    )
    params.update(overrides)
    return await analytics_raw(db=session, **params)


@pytest.mark.asyncio
async def test_raw_returns_rows_and_filters_by_status(session):
    await _write(session, status="done")
    await _write(session, status="failed", engine_version=None, engine_device=None)

    assert len((await _raw(session))["rows"]) == 2
    only_failed = await _raw(session, status="failed")
    assert len(only_failed["rows"]) == 1
    assert only_failed["rows"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_raw_filters_by_device_and_line_category(session):
    await _write(session, engine_device="gpu", alto_lines=35)  # normal
    await _write(session, engine_device="cpu", alto_lines=400)  # very_dense

    gpu = await _raw(session, engine_device="gpu")
    assert len(gpu["rows"]) == 1 and gpu["rows"][0]["engine_device"] == "gpu"

    dense = await _raw(session, line_category="very_dense")
    assert len(dense["rows"]) == 1
    assert dense["rows"][0]["alto_lines"] == 400


@pytest.mark.asyncio
async def test_raw_pagination_has_next(session):
    for i in range(51):
        await _write(session, submitted=BASE + timedelta(seconds=i))
    page1 = await _raw(session, page=1)
    assert len(page1["rows"]) == 50
    assert page1["has_next"] is True
    page2 = await _raw(session, page=2)
    assert len(page2["rows"]) == 1
    assert page2["has_next"] is False


# --- /analytics/breakdown ---


async def _breakdown(session, **overrides):
    params = dict(
        from_date=BASE - timedelta(days=1), to_date=BASE + timedelta(days=1),
        granularity="day", domain=None, engine_device=None, engine_version=None,
        username=None, page=1,
    )
    params.update(overrides)
    return await analytics_breakdown(db=session, **params)


@pytest.mark.asyncio
async def test_breakdown_groups_and_counts(session):
    await _write(session, status="done", started=BASE, finished=BASE + timedelta(seconds=4))
    await _write(session, status="done", started=BASE, finished=BASE + timedelta(seconds=6))
    await _write(session, status="failed", engine_version="1.4.0", engine_device="gpu")

    out = await _breakdown(session)
    # All three share (day, alice, 1.4.0, gpu, mzk) -> one bucket row.
    assert len(out["rows"]) == 1
    r = out["rows"][0]
    assert r["jobs_total"] == 3
    assert r["jobs_done"] == 2
    assert r["jobs_failed"] == 1
    assert r["proc_avg_s"] == pytest.approx(5.0)  # (4 + 6) / 2
    assert out["has_next"] is False  # single bucket, no next page


@pytest.mark.asyncio
async def test_breakdown_pagination_has_next(session):
    # 51 distinct day-buckets (one row each) -> page 1 full + has_next, page 2 has 1.
    for i in range(51):
        await _write(session, submitted=BASE + timedelta(days=i))
    window = dict(from_date=BASE - timedelta(days=1), to_date=BASE + timedelta(days=52))
    page1 = await _breakdown(session, page=1, **window)
    assert len(page1["rows"]) == 50
    assert page1["has_next"] is True
    page2 = await _breakdown(session, page=2, **window)
    assert len(page2["rows"]) == 1
    assert page2["has_next"] is False


@pytest.mark.asyncio
async def test_breakdown_rejects_invalid_granularity(session):
    with pytest.raises(HTTPException) as exc:
        await _breakdown(session, granularity="fortnight")
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_breakdown_rejects_too_many_buckets(session):
    # 600 days at day granularity > 500-bucket cap.
    with pytest.raises(HTTPException) as exc:
        await _breakdown(
            session, from_date=BASE, to_date=BASE + timedelta(days=600), granularity="day"
        )
    assert exc.value.status_code == 400


# --- /analytics/raw.csv ---


async def _csv_body(resp):
    chunks = [c async for c in resp.body_iterator]
    return "".join(c.decode() if isinstance(c, bytes) else c for c in chunks)


@pytest.mark.asyncio
async def test_raw_csv_streams_header_and_rows(session):
    await _write(session, alto_lines=35, mean_conf=0.91)
    await _write(session, status="failed", engine_version=None, engine_device=None)

    resp = await analytics_raw_csv(
        db=session, from_date=None, to_date=None, username=None, domain=None,
        engine_device=None, engine_version=None, status=None, line_category=None,
        block_category=None, char_category=None,
    )
    body = await _csv_body(resp)
    lines = [ln for ln in body.splitlines() if ln.strip()]
    assert lines[0].startswith("submitted_at,job_id,external_id,username,status,fmt")
    assert len(lines) == 1 + 2  # header + two data rows
