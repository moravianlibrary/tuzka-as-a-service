"""Redis round-trip cost of the dispatch backpressure read."""

import asyncio
import os

import pytest
import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.services.redis_jobs import PENDING_HARVEST_KEY, unstored_depth


@pytest.fixture
def unstored_store():
    runner = asyncio.Runner()
    r = aioredis.from_url(os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15"))
    try:
        runner.run(r.ping())
    except (RedisError, OSError):
        runner.run(r.aclose())
        runner.close()
        pytest.skip("Redis not reachable")
    runner.run(r.flushdb())
    runner.run(r.sadd("jobs:inflight", "running"))
    runner.run(r.zadd(PENDING_HARVEST_KEY, {"storing": 1}))
    yield runner, r
    runner.run(r.flushdb())
    runner.run(r.aclose())
    runner.close()


def test_unstored_depth_sequential(benchmark, unstored_store) -> None:
    runner, r = unstored_store

    async def read() -> tuple[int, int]:
        return int(await r.scard("jobs:inflight")), int(await r.zcard(PENDING_HARVEST_KEY))

    assert benchmark(lambda: runner.run(read())) == (1, 1)


def test_unstored_depth_pipelined(benchmark, unstored_store) -> None:
    runner, r = unstored_store
    assert benchmark(lambda: runner.run(unstored_depth(r))) == (1, 1)
