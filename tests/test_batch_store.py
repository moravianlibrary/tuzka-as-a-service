"""Tests for batch progress: the pure ETA, and the counting query and the conditional
finish against a throwaway migrated Postgres (``TEST_DATABASE_URL``; skips otherwise)."""

import os
import uuid

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services import batch_store
from app.services.batch_store import estimate_eta

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


def test_estimate_eta() -> None:
    assert estimate_eta(100, 6.0, 4) == 150.0
    assert estimate_eta(100, 6.0, 0) == 600.0  # no slots yet: one at a time
    assert estimate_eta(100, None, 4) is None


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
                "TRUNCATE batches, backend_domains, domains, job_results, jobs, backends, users "
                "RESTART IDENTITY CASCADE"
            )
        )
        await s.execute(text("INSERT INTO users (username, hashed_key) VALUES ('alice', 'x')"))
        await s.commit()
        yield s
    await engine.dispose()


async def _batch(session, statuses: list[str]) -> uuid.UUID:
    batch_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO batches (id, username, domain, outputs, status, total) "
            "VALUES (:id, 'alice', 'default', '{alto,txt}', 'queued', :n)"
        ),
        {"id": batch_id, "n": len(statuses)},
    )
    for i, status in enumerate(statuses):
        await session.execute(
            text(
                "INSERT INTO jobs (id, username, status, batch_id, source_name) "
                "VALUES (:id, 'alice', :st, :b, :name)"
            ),
            {"id": uuid.uuid4(), "st": status, "b": batch_id, "name": f"p{i}.jpg"},
        )
    await session.commit()
    return batch_id


async def _status(session, batch_id) -> str:
    query = text("SELECT status FROM batches WHERE id = :id")
    return (await session.execute(query, {"id": batch_id})).scalar_one()


@pytest.mark.asyncio
async def test_page_counts(session) -> None:
    batch_id = await _batch(session, ["done", "done", "failed", "running", "queued"])
    counts = await batch_store.page_counts(session, batch_id)
    assert counts == {"done": 2, "failed": 1, "running": 1, "queued": 1}


@pytest.mark.asyncio
async def test_finishes_only_when_no_page_is_left(session) -> None:
    batch_id = await _batch(session, ["done", "running"])
    await batch_store.finish_if_done(session, batch_id)
    await session.commit()
    assert await _status(session, batch_id) == "queued"

    await session.execute(
        text("UPDATE jobs SET status = 'failed' WHERE batch_id = :b"), {"b": batch_id}
    )
    await batch_store.finish_if_done(session, batch_id)
    await session.commit()
    assert await _status(session, batch_id) == "finished"
