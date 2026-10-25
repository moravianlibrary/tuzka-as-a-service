"""Tests for the engine-done → durable harvest queue transition.

Releasing the engine slot when the engine reports done (instead of after the harvest)
is what lets the backend start the next job while the previous result is still being
fetched and stored. These pin that the slot, load counters and batch affinity are
released exactly once, that the job survives on the queue until its harvest finishes
(or fails for good), and that a retry is backed off.
"""

import pytest
import redis.asyncio as aioredis

from app.schemas.redis import BatchCounts, EngineRun, JobState
from app.services.redis_jobs import (
    _SUBMIT_WAKEUP_KEY,
    LOAD_INFLIGHT_KEY,
    PENDING_HARVEST_KEY,
    due_harvest_ids,
    enqueue_job,
    get_backend_batches,
    get_job,
    init_batch,
    next_harvest_at,
    pending_harvest_depth,
    pending_harvest_members,
    release_done,
    reschedule_harvest,
    set_done,
    set_failed,
    set_running,
    unstored_depth,
)
from tests.factories import job_state

TTL = 3600
FIELD = "default|alto,txt"


def _meta(batch_id: str | None = None, backend_id: int = 1) -> JobState:
    return job_state(batch_id=batch_id, backend_id=backend_id)


@pytest.mark.asyncio
async def test_release_done_frees_the_slot_and_queues_the_harvest(
    redis_client: aioredis.Redis,
) -> None:
    await enqueue_job(redis_client, "j1", _meta(), TTL)
    await set_running(redis_client, "j1", "e1", "http://e", 1, TTL)
    await redis_client.delete(_SUBMIT_WAKEUP_KEY)
    snapshot = await get_job(redis_client, "j1")
    assert snapshot is not None
    assert await unstored_depth(redis_client) == (1, 0)

    run = EngineRun(
        started_at="2026-10-22T10:00:00",
        finished_at="2026-10-22T10:00:02",
        metrics={"n_lines": 42},
    )
    assert await release_done(redis_client, "j1", snapshot, run) is True

    assert await redis_client.sismember("jobs:inflight", "j1") == 0
    assert await pending_harvest_depth(redis_client) == 1
    assert await unstored_depth(redis_client) == (0, 1)
    assert await redis_client.get("backend:1:inflight") == b"0"
    assert await redis_client.hget(LOAD_INFLIGHT_KEY, FIELD) == b"0"
    assert await redis_client.llen(_SUBMIT_WAKEUP_KEY) == 1  # the freed slot wakes submit
    stored = await get_job(redis_client, "j1")
    assert stored is not None
    assert stored.status == "storing"
    assert stored.harvest_times == run
    assert await due_harvest_ids(redis_client, 10) == ["j1"]
    assert await next_harvest_at(redis_client) is not None
    assert await pending_harvest_members(redis_client, ["j1", "other"]) == {"j1"}

    # A duplicate done-poll changes nothing: the slot is released (and counted) once.
    assert await release_done(redis_client, "j1", snapshot, run) is False
    assert await redis_client.get("backend:1:inflight") == b"0"
    assert await redis_client.hget(LOAD_INFLIGHT_KEY, FIELD) == b"0"


@pytest.mark.asyncio
async def test_harvest_done_counts_the_batch_page_once(
    redis_client: aioredis.Redis,
) -> None:
    await init_batch(redis_client, BatchCounts(batch_id="b1", total=2), TTL)
    for job_id in ("p1", "p2"):
        await enqueue_job(redis_client, job_id, _meta(batch_id="b1"), TTL)
        await set_running(redis_client, job_id, "e", "http://e", 1, TTL)
    assert await get_backend_batches(redis_client, 1) == {"b1": 2}

    snapshot = await get_job(redis_client, "p1")
    assert snapshot is not None
    await release_done(redis_client, "p1", snapshot, {})

    # The engine slot and the batch affinity are gone at engine-done, but the page only
    # counts as done once its results are stored.
    assert await get_backend_batches(redis_client, 1) == {"b1": 1}
    assert await redis_client.hget("batch:b1", "done") == b"0"
    assert await set_done(redis_client, "p1", TTL, snapshot) == BatchCounts(
        batch_id="b1", total=2, done=1, failed=0
    )
    assert await pending_harvest_depth(redis_client) == 0
    assert await redis_client.exists("job:p1") == 1
    assert await redis_client.zscore(PENDING_HARVEST_KEY, "p1") is None
    assert await redis_client.hget("job:p1", "harvest_times") is None
    # A re-run after the harvest doesn't count the page again.
    assert await set_done(redis_client, "p1", TTL) is None


@pytest.mark.asyncio
async def test_failed_harvest_leaves_the_queue_and_counts_once(
    redis_client: aioredis.Redis,
) -> None:
    await init_batch(redis_client, BatchCounts(batch_id="b2", total=1), TTL)
    await enqueue_job(redis_client, "p3", _meta(batch_id="b2"), TTL)
    await set_running(redis_client, "p3", "e", "http://e", 1, TTL)
    snapshot = await get_job(redis_client, "p3")
    assert snapshot is not None
    await release_done(redis_client, "p3", snapshot, {})

    assert await set_failed(redis_client, "p3", "engine gone", TTL) == BatchCounts(
        batch_id="b2", total=1, done=0, failed=1
    )
    assert await pending_harvest_depth(redis_client) == 0
    # The release already took the job out of inflight; the failed path must not
    # decrement the load counter a second time.
    assert await redis_client.hget(LOAD_INFLIGHT_KEY, FIELD) in (None, b"0")
    assert await set_failed(redis_client, "p3", "engine gone", TTL) is None


@pytest.mark.asyncio
async def test_reschedule_harvest_backs_off(redis_client: aioredis.Redis) -> None:
    await enqueue_job(redis_client, "j5", _meta(), TTL)
    await set_running(redis_client, "j5", "e", "http://e", 1, TTL)
    snapshot = await get_job(redis_client, "j5")
    assert snapshot is not None
    await release_done(redis_client, "j5", snapshot, {})
    before = await next_harvest_at(redis_client)
    assert before is not None

    await reschedule_harvest(redis_client, "j5", 30)

    job = await get_job(redis_client, "j5")
    assert job is not None
    assert job.harvest_attempts == 1
    after = await next_harvest_at(redis_client)
    assert after is not None
    assert after >= before + 25
    assert await due_harvest_ids(redis_client, 10) == []
