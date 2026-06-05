import os

import pytest
import redis.asyncio as aioredis

# db 15 keeps test keys away from the dev stack's db 0
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
async def redis_client():
    r = aioredis.from_url(TEST_REDIS_URL, decode_responses=False)
    try:
        await r.ping()
    except Exception:
        pytest.skip("Redis not reachable — run `docker compose up -d redis`")
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()
