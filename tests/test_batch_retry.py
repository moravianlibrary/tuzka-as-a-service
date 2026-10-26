"""Pins what a batch retry enqueues: a retried page keeps its megapixels, which dispatch
scoring and the per-backend speed both divide by. Against a throwaway migrated Postgres
(``TEST_DATABASE_URL``) and Redis; skips otherwise."""

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from sqlalchemy import select, text

from app.models.job import Job
from app.routers import batches


async def _finished_batch_with_failed_page(session, megapixels: float) -> uuid.UUID:
    batch_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO batches (id, username, domain, outputs, status, total) "
            "VALUES (:id, 'alice', 'default', '{alto,txt}', 'finished', 1)"
        ),
        {"id": batch_id},
    )
    await session.execute(
        text(
            "INSERT INTO jobs (id, username, status, batch_id, source_name, megapixels) "
            "VALUES (:id, 'alice', 'failed', :b, 'p0.jpg', :mp)"
        ),
        {"id": uuid.uuid4(), "b": batch_id, "mp": megapixels},
    )
    await session.commit()
    return batch_id


async def test_retried_page_keeps_its_megapixels(session, redis_client, monkeypatch) -> None:
    batch_id = await _finished_batch_with_failed_page(session, megapixels=4.0)
    monkeypatch.setattr(batches.storage, "copy_object", AsyncMock(return_value=True))
    request = SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(incoming_client=MagicMock()))
    )

    response = await batches.retry_batch(
        request=request,
        batch_id=batch_id,
        username="alice",
        db=session,
        r=redis_client,
        settings=SimpleNamespace(minio_incoming_bucket="incoming"),
    )

    assert response.retried == 1
    retried = await session.scalar(
        select(Job).where(Job.batch_id == batch_id, Job.status == "queued")
    )
    assert retried is not None
    assert retried.megapixels == 4.0  # the row the poller records speed with
    stored = await redis_client.hget(f"job:{retried.id}", "mp")
    assert stored is not None and float(stored) == 4.0  # what dispatch scores with
