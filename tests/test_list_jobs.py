"""GET /api/v1/jobs input checks that run before the query."""

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.routers.jobs import get_job_status, list_jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["nul\x00byte", "line\nbreak"])
async def test_unprintable_external_id_filter_matches_nothing(label) -> None:
    # Submit refuses such labels; and a NUL sent to Postgres was a 500. No query runs.
    resp = await list_jobs(
        status=None, external_id=label, limit=50, offset=0, username="alice", db=None
    )
    assert resp.jobs == [] and resp.total == 0


@pytest.mark.asyncio
async def test_observed_engine_completion_is_visible_and_filterable() -> None:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to a throwaway migrated DB")
    engine = create_async_engine(url)
    job_id = uuid.uuid4()
    username = f"storing-{job_id}"
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as db:
            await db.execute(
                text("INSERT INTO users (username, hashed_key) VALUES (:u, 'x')"),
                {"u": username},
            )
            await db.execute(
                text("INSERT INTO jobs (id, username, status) VALUES (:id, :u, 'storing')"),
                {"id": job_id, "u": username},
            )
            await db.commit()

            status = await get_job_status(job_id, username=username, db=db)
            listed = await list_jobs(
                status="storing",
                external_id=None,
                limit=50,
                offset=0,
                username=username,
                db=db,
            )
            assert status.status == "storing"
            assert listed.total == 1
            assert [job.job_id for job in listed.jobs] == [job_id]
    finally:
        await engine.dispose()
