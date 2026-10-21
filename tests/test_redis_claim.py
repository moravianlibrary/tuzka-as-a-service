"""Peek-and-claim dispatch: a pending job leaves its queue only when a submit worker
claims it, and the cleanup worker puts back a claimed job whose worker died.

Uses the ``redis_client`` fixture (skips if Redis is not reachable)."""

import time

import pytest
import redis.asyncio as aioredis

from app.services.redis_jobs import (
    claim_pending,
    enqueue_job,
    peek_pending,
    requeue_orphans,
    schedule_retry,
    set_running,
)

TTL = 3600


async def _enqueue(r: aioredis.Redis, job_id: str, priority: int, submitted_at: float) -> None:
    meta = {
        "username": "alice",
        "fmt": "multi",
        "outputs": "alto,txt",
        "submitted_at": str(submitted_at),
        "priority": str(priority),
    }
    await enqueue_job(r, job_id, meta, TTL)


@pytest.mark.asyncio
async def test_peek_orders_by_priority_then_age_and_removes_nothing(
    redis_client: aioredis.Redis,
) -> None:
    await _enqueue(redis_client, "old", 0, 100.0)
    await _enqueue(redis_client, "new", 0, 200.0)
    await _enqueue(redis_client, "urgent", 5, 300.0)

    assert [e[0] for e in await peek_pending(redis_client, 2)] == ["urgent", "old"]
    # Peeking again sees the same jobs: nothing left the queue.
    assert await peek_pending(redis_client, 10) == [
        ("urgent", 300.0, 5),
        ("old", 100.0, 0),
        ("new", 200.0, 0),
    ]


@pytest.mark.asyncio
async def test_a_job_is_claimed_once(redis_client: aioredis.Redis) -> None:
    await _enqueue(redis_client, "j", 0, 100.0)

    assert await claim_pending(redis_client, "j", 0) is True
    assert await claim_pending(redis_client, "j", 0) is False  # another worker was first
    assert await peek_pending(redis_client, 10) == []
    assert await redis_client.hget("job:j", "claimed_at") is not None


@pytest.mark.asyncio
async def test_requeue_orphans_puts_back_only_lost_jobs(redis_client: aioredis.Redis) -> None:
    now = time.time()
    for job_id in ("lost", "fresh", "waiting", "delayed", "running"):
        await _enqueue(redis_client, job_id, 0, 100.0)
    for job_id in ("lost", "fresh", "delayed", "running"):
        await claim_pending(redis_client, job_id, 0)
    await redis_client.hset("job:lost", "claimed_at", str(now - 600))  # its worker died
    await schedule_retry(redis_client, "delayed", 100.0, 30.0, None)
    await set_running(redis_client, "running", "e1", "http://e1", 1, TTL)

    ids = ["lost", "fresh", "waiting", "delayed", "running", "unknown"]
    assert await requeue_orphans(redis_client, ids, now) == ["lost"]
    assert [e[0] for e in await peek_pending(redis_client, 10)] == ["lost", "waiting"]
    # Already back on its queue: a second sweep leaves it alone.
    assert await requeue_orphans(redis_client, ids, now) == []
