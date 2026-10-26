"""The shared Redis client of a process (the API, each worker)."""

import redis.asyncio as aioredis

from app.config import Settings


def make_redis(settings: Settings) -> aioredis.Redis:
    """A client whose pool waits for a free connection instead of failing: redis-py 8's
    default pool raises ``MaxConnectionsError`` at 100 connections in use, which a
    poll pass over hundreds of in-flight jobs reached, and the poller crash-looped."""
    pool = aioredis.BlockingConnectionPool.from_url(
        settings.redis_url,
        max_connections=settings.redis_max_connections,
        timeout=settings.redis_pool_timeout_seconds,
    )
    return aioredis.Redis(connection_pool=pool, decode_responses=False)
