"""Tests for job_attempts — the SQL append helper and attempt_entry shape.

Covers the attempt dict shape (D8) and that append_attempt produces a
working ``update(Job).values(...)`` payload.
"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.clock import utcnow
from app.models.job import Job
from app.services.job_attempts import append_attempt
from app.services.retry_policy import attempt_entry

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


# ---- attempt_entry shape (D8) ----


def test_attempt_entry_has_d8_shape() -> None:
    entry = attempt_entry(
        stage="dispatch",
        backend_id=5,
        http_status=502,
        error="HTTP 502",
        retry=True,
        at="2026-06-01T12:00:00",
    )
    assert entry == {
        "at": "2026-06-01T12:00:00",
        "backend_id": 5,
        "error": "HTTP 502",
        "http_status": 502,
        "retry": True,
        "stage": "dispatch",
    }


def test_attempt_entry_null_fields() -> None:
    entry = attempt_entry(
        stage="poll",
        backend_id=None,
        http_status=None,
        error="connection refused",
        retry=False,
        at="2026-06-01T12:00:00",
    )
    assert entry["backend_id"] is None
    assert entry["http_status"] is None
    assert entry["retry"] is False


# ---- DB test: append_attempt ----


@pytest.fixture
async def session():
    if not TEST_DATABASE_URL:
        pytest.skip("Set TEST_DATABASE_URL to a throwaway migrated DB to run job_attempts tests")
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
        await s.execute(text("INSERT INTO users (username, hashed_key) VALUES ('alice', 'x')"))
        await s.execute(text("INSERT INTO backends (id, url) VALUES (1, 'http://e')"))
        await s.commit()
        yield s
    await engine.dispose()


async def test_append_attempt_adds_entries(session) -> None:
    """Insert a job, append two attempts via append_attempt, read back 2 entries in order."""
    job_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO jobs (id, username, external_id, status, submitted_at) "
            "VALUES (:jid, 'alice', :eid, 'failed', :now)"
        ),
        {"jid": job_id, "eid": f"ext-{uuid.uuid4()}", "now": utcnow()},
    )
    await session.commit()

    entry1 = attempt_entry(
        stage="dispatch",
        backend_id=1,
        http_status=502,
        error="HTTP 502 Bad Gateway",
        retry=True,
        at="2026-06-01T12:00:00",
    )
    entry2 = attempt_entry(
        stage="poll",
        backend_id=1,
        http_status=None,
        error="connection refused",
        retry=True,
        at="2026-06-01T12:00:01",
    )

    for entry in (entry1, entry2):
        await session.execute(update(Job).where(Job.id == job_id).values(**append_attempt(entry)))
        await session.commit()

    # Read back and verify
    result = await session.execute(select(Job.attempts).where(Job.id == job_id))
    attempts = result.scalar_one()
    assert len(attempts) == 2
    assert attempts[0]["stage"] == "dispatch"
    assert attempts[0]["http_status"] == 502
    assert attempts[1]["stage"] == "poll"
    assert attempts[1]["error"] == "connection refused"
