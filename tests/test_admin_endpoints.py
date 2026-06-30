"""Integration tests for the guarded hard-delete endpoints and the CSV export.

Like the other DB-backed tests, these need a throwaway migrated Postgres via
``TEST_DATABASE_URL`` and skip otherwise.
"""

import os
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.routers.admin import delete_backend, delete_user
from app.routers.dashboard import download_stats_csv
from app.services.analytics import write_analytics_row

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


@pytest.fixture
async def session():
    if not TEST_DATABASE_URL:
        pytest.skip("Set TEST_DATABASE_URL to a throwaway migrated DB to run these tests")
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
        await s.commit()
        yield s
    await engine.dispose()


async def _user(session, name):
    await session.execute(
        text("INSERT INTO users (username, hashed_key) VALUES (:u, 'x')"), {"u": name}
    )
    await session.commit()


async def _backend(session, bid):
    await session.execute(
        text("INSERT INTO backends (id, url) VALUES (:i, :u)"),
        {"i": bid, "u": f"http://e{bid}"},
    )
    await session.commit()


async def _job_for(session, username, backend_id):
    await session.execute(
        text(
            "INSERT INTO jobs (id, username, external_id, status, backend_id, finished_at) "
            "VALUES (:id, :u, :ext, 'done', :b, now())"
        ),
        {"id": uuid.uuid4(), "u": username, "ext": uuid.uuid4(), "b": backend_id},
    )
    await session.commit()


@pytest.mark.asyncio
async def test_delete_user_blocked_while_jobs_exist(session):
    await _user(session, "alice")
    await _backend(session, 1)
    await _job_for(session, "alice", 1)

    with pytest.raises(HTTPException) as exc:
        await delete_user("alice", session)
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_delete_user_succeeds_without_jobs(session):
    await _user(session, "carol")
    res = await delete_user("carol", session)
    assert res["status"] == "deleted"
    left = await session.execute(
        text("SELECT count(*) FROM users WHERE username='carol'")
    )
    assert left.scalar() == 0


@pytest.mark.asyncio
async def test_delete_backend_blocked_then_allowed(session):
    await _user(session, "alice")
    await _backend(session, 1)
    await _job_for(session, "alice", 1)
    with pytest.raises(HTTPException) as exc:
        await delete_backend(1, session)
    assert exc.value.status_code == 409

    await _backend(session, 2)  # no jobs reference backend 2
    res = await delete_backend(2, session)
    assert res["status"] == "deleted"


@pytest.mark.asyncio
async def test_stats_csv_aggregates_job_analytics(session):
    year = datetime.utcnow().year
    submitted = datetime(year, 1, 15, 12, 0, 0)
    await _user(session, "alice")
    await _backend(session, 1)
    # Two done jobs (durations 4s and 6s) + one failed, same user/engine/domain/day.
    for dur in (4, 6):
        await write_analytics_row(
            session,
            job_id=uuid.uuid4(), external_id=uuid.uuid4(), submitted_at=submitted,
            username="alice", engine_version="1.0.0", engine_device="gpu", backend_id=1,
            domain="default", fmt="multi", status="done", file_size_bytes=1000,
            dispatched_at=submitted, engine_received_at=submitted, started_at=submitted,
            finished_at=submitted + timedelta(seconds=dur), stored_at=submitted + timedelta(seconds=dur),
        )
    await write_analytics_row(
        session,
        job_id=uuid.uuid4(), external_id=uuid.uuid4(), submitted_at=submitted,
        username="alice", engine_version="1.0.0", engine_device="gpu", backend_id=1,
        domain="default", fmt="multi", status="failed", file_size_bytes=1000,
        dispatched_at=None, engine_received_at=None, started_at=None, finished_at=None, stored_at=None,
    )
    await session.commit()

    resp = await download_stats_csv(year=year, db=session)
    chunks = [c async for c in resp.body_iterator]
    body = "".join(c if isinstance(c, str) else c.decode() for c in chunks)

    lines = [ln for ln in body.splitlines() if ln.strip()]
    assert lines[0].startswith("stat_date,username,engine_version,domain")
    # One aggregated row for (alice, 1.0.0, default) on the day.
    data = [ln for ln in lines[1:] if "alice" in ln]
    assert len(data) == 1
    cols = data[0].split(",")
    # jobs_total, jobs_done, jobs_failed, proc_count, proc_avg_seconds
    assert cols[4] == "3"   # jobs_total
    assert cols[5] == "2"   # jobs_done
    assert cols[6] == "1"   # jobs_failed
    assert cols[7] == "2"   # proc_count (only the two done jobs have a duration)
    assert float(cols[8]) == pytest.approx(5.0)  # proc_avg_seconds = (4+6)/2
