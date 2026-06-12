"""Integration test for the retention rollup (cleanup worker).

Needs a real Postgres (percentile_cont / FILTER / ON CONFLICT / advisory lock are
Postgres-specific), pointed at a throwaway, already-migrated database via
``TEST_DATABASE_URL``. Skips when that isn't set or isn't reachable — same spirit as
the Redis-backed tests.
"""

import os
import uuid
from datetime import datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.workers.cleanup import rollup_and_delete

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
async def session():
    if not TEST_DATABASE_URL:
        pytest.skip("Set TEST_DATABASE_URL to a throwaway migrated DB to run rollup tests")
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
                "TRUNCATE job_results, jobs, job_daily_stats, backends, users "
                "RESTART IDENTITY CASCADE"
            )
        )
        await s.execute(
            text("INSERT INTO users (username, hashed_key) VALUES ('alice', 'x'), ('bob', 'x')")
        )
        await s.execute(text("INSERT INTO backends (id, url) VALUES (1, 'http://e')"))
        await s.commit()
        yield s
    await engine.dispose()


async def _add_job(session, *, username, finished_days_ago, duration_s, status="done",
                   engine_version="1.0.0", domain="default", requeues=0):
    finished = datetime.utcnow() - timedelta(days=finished_days_ago)
    started = finished - timedelta(seconds=duration_s) if duration_s is not None else None
    await session.execute(
        text(
            "INSERT INTO jobs (id, username, external_id, status, fmt, domain, "
            "engine_version, backend_id, requeues, submitted_at, started_at, finished_at) "
            "VALUES (:id, :u, :ext, :st, 'multi', :dom, :ev, 1, :rq, :sub, :start, :fin)"
        ),
        {
            "id": uuid.uuid4(), "u": username, "ext": uuid.uuid4(), "st": status,
            "dom": domain, "ev": engine_version, "rq": requeues,
            "sub": started or finished, "start": started, "fin": finished,
        },
    )


async def _stats(session, username, engine_version="1.0.0", domain="default"):
    row = await session.execute(
        text(
            "SELECT jobs_total, jobs_done, jobs_failed, requeues_total, proc_count, "
            "proc_avg_seconds, proc_stddev_seconds, proc_min_seconds, proc_max_seconds, "
            "proc_p50_seconds, proc_p95_seconds, proc_p99_seconds "
            "FROM job_daily_stats WHERE username=:u AND engine_version=:ev AND domain=:d"
        ),
        {"u": username, "ev": engine_version, "d": domain},
    )
    return row.mappings().all()


@pytest.mark.asyncio
async def test_rollup_aggregates_aged_days_and_keeps_recent(session):
    # Aged-out day (40 days back): durations 1..10s done, plus one failed (no duration)
    # and a requeue, all in one (alice, 1.0.0, default) group.
    for d in range(1, 11):
        await _add_job(session, username="alice", finished_days_ago=40, duration_s=d)
    await _add_job(session, username="alice", finished_days_ago=40, duration_s=None,
                   status="failed", requeues=3)
    # A second group on the aged day — different engine_version — must stay separate.
    await _add_job(session, username="bob", finished_days_ago=40, duration_s=2,
                   engine_version="2.0.0")
    # Recent day (5 days back) — must NOT be rolled up or deleted.
    await _add_job(session, username="alice", finished_days_ago=5, duration_s=4)
    await session.commit()

    await rollup_and_delete(session)

    # Recent job survives; aged jobs are gone.
    remaining = await session.execute(text("SELECT count(*) FROM jobs"))
    assert remaining.scalar() == 1

    rows = await _stats(session, "alice")
    assert len(rows) == 1
    s = rows[0]
    assert s["jobs_total"] == 11
    assert s["jobs_done"] == 10
    assert s["jobs_failed"] == 1
    assert s["requeues_total"] == 3
    assert s["proc_count"] == 10  # failed job (no duration) excluded from timing
    assert s["proc_avg_seconds"] == pytest.approx(5.5)
    assert s["proc_min_seconds"] == pytest.approx(1.0)
    assert s["proc_max_seconds"] == pytest.approx(10.0)
    assert s["proc_stddev_seconds"] == pytest.approx(2.8722813, abs=1e-5)
    assert s["proc_p50_seconds"] == pytest.approx(5.5)
    assert s["proc_p95_seconds"] == pytest.approx(9.55, abs=1e-6)
    assert s["proc_p99_seconds"] == pytest.approx(9.91, abs=1e-6)

    # Separate engine_version is its own row.
    assert len(await _stats(session, "bob", engine_version="2.0.0")) == 1

    # Idempotent: a second sweep changes nothing (no double counting, recent intact).
    await rollup_and_delete(session)
    rows2 = await _stats(session, "alice")
    assert rows2[0]["jobs_total"] == 11
    remaining2 = await session.execute(text("SELECT count(*) FROM jobs"))
    assert remaining2.scalar() == 1
