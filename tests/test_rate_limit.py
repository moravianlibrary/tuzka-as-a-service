"""Pins the token-bucket rate limiter: burst/deny, Retry-After accuracy, per-user/class isolation, key expiry."""

import asyncio
import math

from app.schemas.runtime_config import RateLimit
from app.services.rate_limit import check

# per_minute=600 -> emission interval T = 0.1 s; burst=2 -> tau = 0.2 s
PER_MINUTE = 600
BURST = 2
LIMIT = RateLimit(per_minute=PER_MINUTE, burst=BURST)
T = 60.0 / PER_MINUTE


async def test_burst_then_deny(redis_client):
    # burst+1 instant requests pass, the next is denied
    for i in range(BURST + 1):
        result = await check(redis_client, "query", "alice", LIMIT)
        assert result.allowed, f"request {i} should be allowed"
    result = await check(redis_client, "query", "alice", LIMIT)
    assert not result.allowed


async def test_retry_after_accuracy(redis_client):
    for _ in range(BURST + 1):
        await check(redis_client, "query", "bob", LIMIT)
    result = await check(redis_client, "query", "bob", LIMIT)
    assert not result.allowed
    assert 0 < result.retry_after <= T + 0.05


async def test_allowed_again_after_waiting(redis_client):
    for _ in range(BURST + 1):
        await check(redis_client, "query", "carol", LIMIT)
    denied = await check(redis_client, "query", "carol", LIMIT)
    assert not denied.allowed
    await asyncio.sleep(denied.retry_after + 0.02)
    result = await check(redis_client, "query", "carol", LIMIT)
    assert result.allowed


async def test_users_and_classes_are_independent(redis_client):
    for _ in range(BURST + 2):
        await check(redis_client, "query", "dave", LIMIT)
    # other user unaffected
    assert (await check(redis_client, "query", "erin", LIMIT)).allowed
    # other class unaffected
    assert (await check(redis_client, "submit", "dave", LIMIT)).allowed


async def test_idle_key_expires(redis_client):
    await check(redis_client, "query", "frank", LIMIT)
    ttl = await redis_client.ttl(b"rl:query:frank")
    assert 0 < ttl <= math.ceil(BURST * T + T) + 1


async def test_nonpositive_rate_denies_cleanly(redis_client):
    # misconfigured limits must not crash the hot path
    result = await check(redis_client, "query", "greg", RateLimit(per_minute=0, burst=BURST))
    assert not result.allowed
    assert result.retry_after == 60.0
    result = await check(redis_client, "query", "greg", RateLimit(per_minute=-10, burst=BURST))
    assert not result.allowed
    # negative burst is clamped to 0 (steady rate, no burst)
    result = await check(redis_client, "query", "hana", RateLimit(per_minute=PER_MINUTE, burst=-5))
    assert result.allowed
