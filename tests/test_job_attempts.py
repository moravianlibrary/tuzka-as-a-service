"""Tests for job_attempts: an Attempt is stored in the D8 JSON shape, and append_attempt
produces a working ``update(Job).values(...)`` payload."""

from __future__ import annotations

import uuid
from datetime import datetime

import pytest
from sqlalchemy import select, text, update

from app.clock import utcnow
from app.models.job import Job
from app.schemas.attempt import Attempt
from app.services.job_attempts import append_attempt
from app.vocabulary import FailureStage

# ---- stored shape (D8) ----


def test_attempt_is_stored_in_d8_shape() -> None:
    attempt = Attempt(
        at=datetime(2026, 6, 1, 12),
        stage=FailureStage.DISPATCH,
        backend_id=5,
        http_status=502,
        error="HTTP 502",
        retry=True,
    )

    stored = Job.attempts.type.process_bind_param([attempt], None)

    assert stored == [
        {
            "at": "2026-06-01T12:00:00",
            "backend_id": 5,
            "error": "HTTP 502",
            "http_status": 502,
            "retry": True,
            "stage": "dispatch",
        }
    ]
    assert Job.attempts.type.process_result_value(stored, None) == [attempt]


# ---- DB test: append_attempt ----


@pytest.fixture
async def session(session):
    """A session on the emptied test database, with ``alice`` and backend 1."""
    await session.execute(text("INSERT INTO backends (id, url) VALUES (1, 'http://e')"))
    await session.commit()
    return session


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

    entry1 = Attempt(
        at=datetime(2026, 6, 1, 12),
        stage=FailureStage.DISPATCH,
        backend_id=1,
        http_status=502,
        error="HTTP 502 Bad Gateway",
        retry=True,
    )
    entry2 = Attempt(
        at=datetime(2026, 6, 1, 12, 0, 1),
        stage=FailureStage.POLL,
        backend_id=1,
        http_status=None,
        error="connection refused",
        retry=True,
    )

    for entry in (entry1, entry2):
        await session.execute(update(Job).where(Job.id == job_id).values(**append_attempt(entry)))
        await session.commit()

    # Read back and verify
    result = await session.execute(select(Job.attempts).where(Job.id == job_id))
    assert result.scalar_one() == [entry1, entry2]
