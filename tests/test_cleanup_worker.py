"""Pins what a cleanup tick does: put lost jobs back on their queue, purge expired
objects on every tenth tick, and keep going when one step fails. Against a throwaway
migrated Postgres (``TEST_DATABASE_URL``) and Redis; skips otherwise. The object storage
is a fake."""

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import pytest
import redis.asyncio as aioredis

from app.clock import utcnow
from app.config import Settings
from app.models.job import Job
from app.services.redis_jobs import enqueue_job, peek_pending
from app.vocabulary import JobStatus
from app.workers.cleanup import HEAVY_EVERY, CleanupWorker
from tests.factories import job_state

TTL = 3600


@dataclass
class FakeObject:
    object_name: str
    last_modified: datetime


class FakeBucket:
    """A bucket's objects by name and age."""

    def __init__(self, objects: dict[str, datetime]) -> None:
        self.objects = objects

    async def list_objects(self, bucket: str, **_: Any) -> AsyncIterator[FakeObject]:
        for name, modified in list(self.objects.items()):
            yield FakeObject(name, modified)

    async def remove_objects(self, bucket: str, objects: list[Any]) -> list[Any]:
        for obj in objects:
            del self.objects[obj._name]
        return []


class BrokenBucket(FakeBucket):
    async def list_objects(self, bucket: str, **_: Any) -> AsyncIterator[FakeObject]:
        raise ConnectionError("storage down")
        yield  # an async generator, like the real listing


def _worker(session_factory, r: aioredis.Redis, incoming: FakeBucket, results: FakeBucket):
    return CleanupWorker(
        Settings(),
        r,
        session_factory,
        incoming,  # type: ignore[arg-type]
        results,  # type: ignore[arg-type]
        asyncio.Event(),
    )


async def _lost_job(session_factory, r: aioredis.Redis) -> str:
    """A queued job that's in no queue: a submit worker claimed it and died."""
    job_id = str(uuid.uuid4())
    async with session_factory() as db:
        db.add(
            Job(
                id=uuid.UUID(job_id),
                username="alice",
                status=JobStatus.QUEUED,
                domain="default",
                outputs=["alto", "txt"],
                submitted_at=utcnow() - timedelta(minutes=5),
            )
        )
        await db.commit()
    await enqueue_job(r, job_id, job_state(submitted_at=time.time() - 300), TTL)
    await r.zrem("jobs:pending:0", job_id)
    return job_id


def _expired() -> datetime:
    return utcnow() - timedelta(days=30)


@pytest.mark.asyncio
async def test_a_tick_puts_a_lost_job_back_on_its_queue(session_factory, redis_client) -> None:
    job_id = await _lost_job(session_factory, redis_client)
    worker = _worker(session_factory, redis_client, FakeBucket({}), FakeBucket({}))

    await worker.run_tick()

    assert [e.job_id for e in await peek_pending(redis_client, 10)] == [job_id]


@pytest.mark.asyncio
async def test_every_tenth_tick_purges_expired_objects(session_factory, redis_client) -> None:
    incoming = FakeBucket({"alice/old.jpg": _expired(), "alice/new.jpg": utcnow()})
    worker = _worker(session_factory, redis_client, incoming, FakeBucket({}))

    await worker.run_tick()  # the first tick is a heavy one
    assert sorted(incoming.objects) == ["alice/new.jpg"]
    incoming.objects["alice/old2.jpg"] = _expired()
    for _ in range(HEAVY_EVERY - 1):
        await worker.run_tick()
    assert sorted(incoming.objects) == ["alice/new.jpg", "alice/old2.jpg"]  # light ticks

    await worker.run_tick()

    assert sorted(incoming.objects) == ["alice/new.jpg"]


@pytest.mark.asyncio
async def test_a_failing_step_does_not_stop_the_tick(session_factory, redis_client) -> None:
    job_id = await _lost_job(session_factory, redis_client)
    incoming = FakeBucket({"alice/old.jpg": _expired()})
    worker = _worker(session_factory, redis_client, incoming, BrokenBucket({}))
    await redis_client.set("backend:7:inflight", 3)  # nothing is in flight on it

    await worker.run_tick()

    assert [e.job_id for e in await peek_pending(redis_client, 10)] == [job_id]
    assert incoming.objects == {}  # purged although the results bucket failed
    assert int(await redis_client.get("backend:7:inflight") or 0) == 0  # rebuilt after it


@pytest.mark.asyncio
async def test_a_bucket_that_cannot_be_listed_does_not_stop_the_other(
    session_factory, redis_client
) -> None:
    results = FakeBucket({"alice/old.xml.zst": _expired()})
    worker = _worker(session_factory, redis_client, BrokenBucket({}), results)

    await worker.run_tick()

    assert results.objects == {}  # purged although the uploads bucket failed first
