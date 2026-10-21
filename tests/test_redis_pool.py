"""The shared Redis client waits for a free connection instead of failing at 100."""

import redis.asyncio as aioredis

from app.config import Settings
from app.services.redis_pool import make_redis


def test_make_redis_uses_a_blocking_pool_sized_from_settings() -> None:
    settings = Settings(redis_max_connections=7, redis_pool_timeout_seconds=3.0)
    client = make_redis(settings)
    pool = client.connection_pool
    assert isinstance(pool, aioredis.BlockingConnectionPool)
    assert pool.max_connections == 7
    assert pool.timeout == 3.0
