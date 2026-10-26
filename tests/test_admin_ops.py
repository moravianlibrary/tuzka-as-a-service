"""Integration tests for the guarded hard deletes in app/services/admin_ops.

Like the other DB-backed tests, these need a throwaway migrated Postgres via
``TEST_DATABASE_URL`` and skip otherwise.
"""

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from app.services import admin_ops


@pytest.fixture
async def session(database):
    """A session on the emptied test database; the tests add their own users."""
    async with database() as s:
        yield s


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
        {"id": uuid.uuid4(), "u": username, "ext": f"ext-{uuid.uuid4()}", "b": backend_id},
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
