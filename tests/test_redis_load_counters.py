"""Tests for the /load counters kept by the Redis state functions (step 4, D1/D2).

The field helpers are pure. The walks use the ``redis_client`` fixture (skip if Redis
is not reachable) and pin that every transition moves the counters exactly once, so a
job that has finished, however it got there, leaves every field at 0.
"""

import pytest
import redis.asyncio as aioredis

from app.schemas.redis import JobState
from app.services.load import load_field, parse_load_field
from app.services.redis_jobs import (
    LOAD_INFLIGHT_KEY,
    LOAD_QUEUED_KEY,
    LOAD_SPP_KEY,
    QueueEntry,
    claim_pending,
    enqueue_job,
    get_job,
    get_jobs,
    rebuild_load_counters,
    record_page_seconds,
    release_and_requeue,
    set_done,
    set_failed,
    set_running,
)
from tests.factories import job_state

TTL = 3600
FIELD = "default|alto,txt"


def _meta(domain: str = "default", outputs: str = "alto,txt") -> JobState:
    return job_state(external_id="x", domain=domain, outputs=frozenset(outputs.split(",")))


async def _counts(r: aioredis.Redis) -> tuple[dict[str, int], dict[str, int]]:
    async def read(key: str) -> dict[str, int]:
        raw = await r.hgetall(key)
        return {k.decode(): int(v) for k, v in raw.items()}

    return await read(LOAD_QUEUED_KEY), await read(LOAD_INFLIGHT_KEY)


# ---- field helpers (pure) ----


def test_load_field_sorts_outputs() -> None:
    assert load_field("default", frozenset({"txt", "alto"})) == FIELD


def test_parse_load_field_round_trips() -> None:
    assert parse_load_field(FIELD) == ("default", frozenset({"alto", "txt"}))
    assert parse_load_field("maps|geojson") == ("maps", frozenset({"geojson"}))


# ---- walks ----


@pytest.mark.asyncio
async def test_enqueue_run_done(redis_client: aioredis.Redis) -> None:
    await enqueue_job(redis_client, "j1", _meta(), TTL)
    assert await _counts(redis_client) == ({FIELD: 1}, {})

    await claim_pending(
        redis_client, [QueueEntry("j1", 100.0, 0)]
    )  # still queued logically until it runs
    await set_running(redis_client, "j1", "e1", "http://e1", 1, TTL)
    assert await _counts(redis_client) == ({FIELD: 0}, {FIELD: 1})

    await set_done(redis_client, "j1", TTL)
    await set_done(redis_client, "j1", TTL)  # a re-run is a no-op
    assert await _counts(redis_client) == ({FIELD: 0}, {FIELD: 0})


@pytest.mark.asyncio
async def test_done_with_the_pollers_snapshot(redis_client: aioredis.Redis) -> None:
    """The poller passes the hash it already holds; the counters still move once."""
    await enqueue_job(redis_client, "j3", _meta(), TTL)
    await set_running(redis_client, "j3", "e1", "http://e1", 1, TTL)
    snapshot = await get_job(redis_client, "j3")

    await set_done(redis_client, "j3", TTL, snapshot)
    assert await _counts(redis_client) == ({FIELD: 0}, {FIELD: 0})
    assert await redis_client.get("backend:1:inflight") == b"0"


@pytest.mark.asyncio
async def test_enqueue_run_release_run_failed(redis_client: aioredis.Redis) -> None:
    await enqueue_job(redis_client, "j2", _meta("kramarky", "alto"), TTL)
    field = "kramarky|alto"
    await set_running(redis_client, "j2", "e1", "http://e1", 1, TTL)
    await set_running(redis_client, "j2", "e1", "http://e1", 1, TTL)  # no-op
    assert await _counts(redis_client) == ({field: 0}, {field: 1})

    await release_and_requeue(redis_client, "j2", 1.0, TTL)
    assert await _counts(redis_client) == ({field: 1}, {field: 0})

    await set_running(redis_client, "j2", "e2", "http://e2", 2, TTL)
    await set_failed(redis_client, "j2", "boom", TTL)
    await set_failed(redis_client, "j2", "boom", TTL)  # no-op
    assert await _counts(redis_client) == ({field: 0}, {field: 0})


@pytest.mark.asyncio
async def test_enqueue_failed_at_dispatch(redis_client: aioredis.Redis) -> None:
    await enqueue_job(redis_client, "j3", _meta(), TTL)
    await set_failed(redis_client, "j3", "image missing", TTL)
    assert await _counts(redis_client) == ({FIELD: 0}, {})


@pytest.mark.asyncio
async def test_delayed_release_counts_as_queued(redis_client: aioredis.Redis) -> None:
    await enqueue_job(redis_client, "j4", _meta(), TTL)
    await set_running(redis_client, "j4", "e1", "http://e1", 1, TTL)
    await release_and_requeue(redis_client, "j4", 1.0, TTL, delay_s=30, avoid_backend_id=1)
    assert await _counts(redis_client) == ({FIELD: 1}, {FIELD: 0})


@pytest.mark.asyncio
async def test_record_page_seconds_keeps_an_average(redis_client: aioredis.Redis) -> None:
    await record_page_seconds(redis_client, "default", 10.0)
    await record_page_seconds(redis_client, "default", 20.0)
    await record_page_seconds(redis_client, "maps", 100.0)
    spp = {k.decode(): float(v) for k, v in (await redis_client.hgetall(LOAD_SPP_KEY)).items()}
    assert spp == {"default": pytest.approx(11.0), "maps": 100.0}


@pytest.mark.asyncio
async def test_rebuild_repairs_drifted_counters(redis_client: aioredis.Redis) -> None:
    await enqueue_job(redis_client, "q1", _meta(), TTL)
    await enqueue_job(redis_client, "q2", _meta("kramarky", "alto"), TTL)
    await enqueue_job(redis_client, "r1", _meta(), TTL)
    await enqueue_job(redis_client, "d1", _meta(), TTL)
    for job_id in ("r1", "d1"):  # dispatched: off the pending queue, then running
        await redis_client.zrem("jobs:pending:0", job_id)
        await set_running(redis_client, job_id, "e1", "http://e1", 1, TTL)
    await release_and_requeue(redis_client, "d1", 1.0, TTL, delay_s=60)  # delayed = queued
    # Corrupt them: a lost decrement, a stale field.
    await redis_client.hset(LOAD_QUEUED_KEY, mapping={FIELD: 99, "gone|txt": 5})
    await redis_client.delete(LOAD_INFLIGHT_KEY)

    await rebuild_load_counters(redis_client)

    assert await _counts(redis_client) == ({FIELD: 2, "kramarky|alto": 1}, {FIELD: 1})


@pytest.mark.asyncio
async def test_rebuild_with_nothing_queued_clears_counters(redis_client: aioredis.Redis) -> None:
    await redis_client.hset(LOAD_QUEUED_KEY, FIELD, 3)
    await rebuild_load_counters(redis_client)
    assert await _counts(redis_client) == ({}, {})


@pytest.mark.asyncio
async def test_get_jobs_reads_many_in_one_round_trip(redis_client: aioredis.Redis) -> None:
    await enqueue_job(redis_client, "g1", _meta("kramarky"), TTL)
    states = await get_jobs(redis_client, ["g1", "missing"])
    assert states["g1"] is not None and states["g1"].domain == "kramarky"
    assert states["missing"] is None
