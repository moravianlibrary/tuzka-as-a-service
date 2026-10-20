"""Integration tests for the guarded hard deletes in app/core/admin_ops.

Like the other DB-backed tests, these need a throwaway migrated Postgres via
``TEST_DATABASE_URL`` and skip otherwise.
"""

import os
import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core import admin_ops

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
                "TRUNCATE backend_domains, domains, "
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
        await admin_ops.delete_user(session, "alice")
    assert exc.value.status_code == 409


@pytest.mark.asyncio
async def test_delete_user_succeeds_without_jobs(session):
    await _user(session, "carol")
    await admin_ops.delete_user(session, "carol")
    left = await session.execute(text("SELECT count(*) FROM users WHERE username='carol'"))
    assert left.scalar() == 0


@pytest.mark.asyncio
async def test_delete_backend_blocked_then_allowed(session):
    await _user(session, "alice")
    await _backend(session, 1)
    await _job_for(session, "alice", 1)
    with pytest.raises(HTTPException) as exc:
        await admin_ops.delete_backend(session, 1)
    assert exc.value.status_code == 409

    await _backend(session, 2)  # no jobs reference backend 2
    await admin_ops.delete_backend(session, 2)
