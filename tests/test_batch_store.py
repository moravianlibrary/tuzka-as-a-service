"""Tests for batch progress: the pure ETA, and the counting query and the conditional
finish against a throwaway migrated Postgres (``TEST_DATABASE_URL``; skips otherwise)."""

import asyncio
import os
import uuid
from datetime import datetime

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


@pytest.mark.asyncio
async def test_storing_page_keeps_batch_open(session) -> None:
    batch_id = await _batch(session, ["done", "storing"])
    await batch_store.finish_if_done(session, batch_id)
    await session.commit()
    assert await _status(session, batch_id) == "queued"


@pytest.mark.asyncio
async def test_a_retried_page_counts_once_with_its_newest_job(session) -> None:
    batch_id = await _batch(session, ["done"])
    for status, submitted in (
        ("failed", datetime(2026, 10, 21, 10)),
        ("queued", datetime(2026, 10, 21, 11)),
    ):
        await session.execute(
            text(
                "INSERT INTO jobs (id, username, status, batch_id, source_name, submitted_at) "
                "VALUES (:id, 'alice', :st, :b, 'retry.jpg', :at)"
            ),
            {"id": uuid.uuid4(), "st": status, "b": batch_id, "at": submitted},
        )
    await session.commit()

    assert await batch_store.page_counts(session, batch_id) == {"done": 1, "queued": 1}
    listed = [
        (page.source_name, page.status) for page in await batch_store.pages(session, batch_id)
    ]
    assert listed == [("p0.jpg", "done"), ("retry.jpg", "queued")]  # by source name


@pytest.mark.asyncio
async def test_last_two_pages_finishing_together_finish_the_batch(session) -> None:
    """Two workers finish the last two pages at once: each updates its page, then
    checks. Without the batch row lock both see the other page still running."""
    batch_id = await _batch(session, ["running", "running"])
    jobs = (
        await session.execute(
            text("SELECT id FROM jobs WHERE batch_id = :b ORDER BY source_name"), {"b": batch_id}
        )
    ).scalars()
    job_a, job_b = list(jobs)
    factory = async_sessionmaker(session.bind, expire_on_commit=False)
    done = text("UPDATE jobs SET status = 'done' WHERE id = :id")
    async with factory() as a, factory() as b:
        await a.execute(done, {"id": job_a})
        await b.execute(done, {"id": job_b})
        await batch_store.finish_if_done(a, batch_id)  # sees b's page still running
        b_check = asyncio.create_task(batch_store.finish_if_done(b, batch_id))
        await asyncio.sleep(0.2)
        assert not b_check.done(), "b must wait for a's commit"
        await a.commit()
        await b_check
        await b.commit()
    assert await _status(session, batch_id) == "finished"


@pytest.mark.asyncio
async def test_finish_stalled_only_finishes_batches_with_nothing_left(session) -> None:
    stalled = await _batch(session, ["done", "failed"])
    busy = await _batch(session, ["done", "running"])
    assert await batch_store.finish_stalled(session) == 1
    assert await _status(session, stalled) == "finished"
    assert await _status(session, busy) == "queued"
