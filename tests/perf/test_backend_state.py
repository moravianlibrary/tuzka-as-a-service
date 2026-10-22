"""Redis round-trip cost of reading backend state on a submit pass."""

import asyncio
import os

import pytest
import redis.asyncio as aioredis
from redis.exceptions import RedisError

from app.services.redis_jobs import (
    get_backend_batches,
    get_backend_inflight,
    get_backends_state,
)


@pytest.fixture
def backend_state_store():
    runner = asyncio.Runner()
    redis_url = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
    r = aioredis.from_url(redis_url)
    ids = list(range(1, 51))

    async def seed() -> None:
        await r.flushdb()
        async with r.pipeline(transaction=False) as pipe:
            for backend_id in ids:
                pipe.set(f"backend:{backend_id}:inflight", 2)
                pipe.hset(f"backend:{backend_id}:batches", mapping={"batch-a": 2})
            await pipe.execute()

    try:
        runner.run(r.ping())
    except (RedisError, OSError):
        runner.run(r.aclose())
        runner.close()
        pytest.skip("Redis not reachable")
    runner.run(seed())
    yield runner, r, ids
    runner.run(r.flushdb())
    runner.run(r.aclose())
    runner.close()


def test_backend_state_sequential_50(benchmark, backend_state_store) -> None:
    runner, r, ids = backend_state_store

    async def read() -> dict[int, tuple[int, dict[str, int]]]:
        return {
            backend_id: (
                await get_backend_inflight(r, backend_id),
                await get_backend_batches(r, backend_id),
            )
            for backend_id in ids
        }

    result = benchmark(lambda: runner.run(read()))
    assert result[1] == (2, {"batch-a": 2})


def test_backend_state_batched_50(benchmark, backend_state_store) -> None:
    runner, r, ids = backend_state_store
    result = benchmark(lambda: runner.run(get_backends_state(r, ids)))
    assert result[1] == (2, {"batch-a": 2})
