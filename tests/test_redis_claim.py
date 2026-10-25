"""Peek-and-claim dispatch: a pending job leaves its queue only when a submit worker
claims it, and the cleanup worker puts back a claimed job whose worker died.

Uses the ``redis_client`` fixture (skips if Redis is not reachable)."""

import time

import pytest
import redis.asyncio as aioredis

from app.services.redis_jobs import (
    claim_pending,
    enqueue_job,
    get_backend_batches,
    get_backend_inflight,
    get_backends_state,
    peek_pending,
    rebuild_backend_counters,
    requeue_orphans,
    schedule_retry,
    set_running,
)
from tests.factories import job_state

TTL = 3600


@pytest.mark.asyncio
async def test_get_backends_state_handles_missing_counters_and_batches(
    redis_client: aioredis.Redis,
) -> None:
    await redis_client.set("backend:1:inflight", 2)
    await redis_client.hset("backend:1:batches", mapping={"batch-a": 2, "batch-b": 1})

    assert await get_backends_state(redis_client, [1, 2]) == {
        1: (2, {"batch-a": 2, "batch-b": 1}),
        2: (0, {}),
    }
    assert await get_backends_state(redis_client, []) == {}


async def _enqueue(r: aioredis.Redis, job_id: str, priority: int, submitted_at: float) -> None:
    await enqueue_job(r, job_id, job_state(submitted_at=submitted_at, priority=priority), TTL)


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


@pytest.mark.asyncio
async def test_rebuild_backend_counters_matches_the_inflight_jobs(
    redis_client: aioredis.Redis,
) -> None:
    for job_id in ("a", "b", "c"):
        await _enqueue(redis_client, job_id, 0, 100.0)
        await claim_pending(redis_client, job_id, 0)
    await redis_client.hset("job:c", "batch_id", "B1")
    await set_running(redis_client, "a", "e1", "http://e1", 1, TTL)
    await set_running(redis_client, "b", "e2", "http://e2", 2, TTL)
    await set_running(redis_client, "c", "e3", "http://e2", 2, TTL)
    # A job dispatched twice: counted on backend 1, recorded (and later released) on 2.
    await redis_client.set("backend:1:inflight", 3)
    await redis_client.set("backend:2:inflight", -1)
    await redis_client.set("backend:7:inflight", 2)  # nothing runs there any more

    assert await rebuild_backend_counters(redis_client) == 3
    assert [await get_backend_inflight(redis_client, b) for b in (1, 2, 7)] == [1, 2, 0]
    assert await get_backend_batches(redis_client, 2) == {"B1": 1}
    assert await rebuild_backend_counters(redis_client) == 0  # already right
