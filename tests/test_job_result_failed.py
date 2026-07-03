"""A failed job surfaces as data, not a 5xx.

`GET /jobs/{job_id}/result` returns 200 with status="failed" (+ error, no results); the
streaming download returns 409. Handlers are called directly, so the failed branch
(which never touches request/storage state) is exercised without an HTTP layer. Needs a
throwaway migrated DB via TEST_DATABASE_URL and skips otherwise.
"""

import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.routers.jobs import download_job_result, get_job_result

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


class _Request:
    """Stand-in: the failed branch returns before reading any request state."""


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
            text("TRUNCATE job_results, jobs, backends, users RESTART IDENTITY CASCADE")
        )
        await s.commit()
        yield s
    await engine.dispose()


async def _failed_job(session, username: str = "alice", error: str = "engine boom") -> uuid.UUID:
    await session.execute(
        text("INSERT INTO users (username, hashed_key) VALUES (:u, 'x')"), {"u": username}
    )
    job_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO jobs (id, username, external_id, status, error, finished_at) "
            "VALUES (:id, :u, :ext, 'failed', :err, now())"
        ),
        {"id": job_id, "u": username, "ext": uuid.uuid4(), "err": error},
    )
    await session.commit()
    return job_id


async def test_result_of_failed_job_is_200_with_status_failed(session) -> None:
    job_id = await _failed_job(session)

    resp = await get_job_result(
        job_id, _Request(), username="alice", db=session, settings=Settings()
    )

    assert resp.status == "failed"  # data, not a 5xx
    assert resp.error == "engine boom"
    assert resp.results == []


async def test_download_of_failed_job_is_409(session) -> None:
    job_id = await _failed_job(session)

    with pytest.raises(HTTPException) as exc:
        await download_job_result(
            job_id, "alto", _Request(), username="alice", db=session, settings=Settings()
        )

    assert exc.value.status_code == 409  # not 500 — the engine failed, not this API
