"""Tests for batch progress counters and events (step 7, D8/D12). The event shapes are
pure; the counters use the ``redis_client`` fixture (skip if Redis is unreachable)."""

import pytest
import redis.asyncio as aioredis

from app.schemas.redis import BatchCounts, JobState
from app.services.redis_jobs import (
    batch_event,
    enqueue_job,
    get_backend_batches,
    init_batch,
    job_event_payload,
    set_done,
    set_failed,
    set_running,
)
from app.vocabulary import JobStatus
from tests.factories import job_state

TTL = 3600


def test_batch_event_shape() -> None:
    assert batch_event(BatchCounts(batch_id="b1", total=3, done=2, failed=1)) == {
        "type": "batch",
        "batch_id": "b1",
        "total": 3,
        "done": 2,
        "failed": 1,
    }


def test_page_events_carry_the_batch_id() -> None:
    assert job_event_payload("j1", None, "done", batch_id="b1")["batch_id"] == "b1"
    assert "batch_id" not in job_event_payload("j2", None, "done")


def _page(batch_id: str | None) -> JobState:
    return job_state(batch_id=batch_id)


@pytest.mark.asyncio
async def test_three_pages_two_done_one_failed(redis_client: aioredis.Redis) -> None:
    await init_batch(redis_client, BatchCounts(batch_id="b1", total=3), TTL)
    for job_id in ("p1", "p2", "p3"):
        await enqueue_job(redis_client, job_id, _page("b1"), TTL)
        await set_running(redis_client, job_id, "e", "http://e", 1, TTL)

    assert await set_done(redis_client, "p1", TTL) == BatchCounts(
        batch_id="b1", total=3, done=1, failed=0
    )
    # A stale snapshot from the poller still must not count the finished page again.
    stale = job_state(batch_id="b1", backend_id=1, status=JobStatus.RUNNING)
    assert await set_done(redis_client, "p1", TTL, stale) is None
    assert await set_failed(redis_client, "p2", "boom", TTL) == BatchCounts(
        batch_id="b1", total=3, done=1, failed=1
    )
    assert await set_done(redis_client, "p3", TTL) == BatchCounts(
        batch_id="b1", total=3, done=2, failed=1
    )
    # A re-run of a state function doesn't count a page twice.
    assert await set_done(redis_client, "p3", TTL) is None


@pytest.mark.asyncio
async def test_single_jobs_have_no_batch_counts(redis_client: aioredis.Redis) -> None:
    await enqueue_job(redis_client, "solo", _page(None), TTL)
    assert await set_failed(redis_client, "solo", "boom", TTL) is None


@pytest.mark.asyncio
async def test_backend_batch_inflight_tracking(redis_client: aioredis.Redis) -> None:
    for job_id in ("a1", "a2"):
        await enqueue_job(redis_client, job_id, _page("b9"), TTL)
        await set_running(redis_client, job_id, "e", "http://e", 1, TTL)
    assert await get_backend_batches(redis_client, 1) == {"b9": 2}

    await set_done(redis_client, "a1", TTL)
    assert await get_backend_batches(redis_client, 1) == {"b9": 1}
    await set_failed(redis_client, "a2", "boom", TTL)
    assert await get_backend_batches(redis_client, 1) == {}
