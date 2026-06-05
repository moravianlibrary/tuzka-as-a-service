import asyncio
import math

from app.services.rate_limit import check

# per_minute=600 -> emission interval T = 0.1 s; burst=2 -> tau = 0.2 s
PER_MINUTE = 600
BURST = 2
T = 60.0 / PER_MINUTE


async def test_burst_then_deny(redis_client):
    # burst+1 instant requests pass, the next is denied
    for i in range(BURST + 1):
        result = await check(redis_client, "query", "alice", PER_MINUTE, BURST)
        assert result.allowed, f"request {i} should be allowed"
    result = await check(redis_client, "query", "alice", PER_MINUTE, BURST)
    assert not result.allowed


async def test_retry_after_accuracy(redis_client):
    for _ in range(BURST + 1):
        await check(redis_client, "query", "bob", PER_MINUTE, BURST)
    result = await check(redis_client, "query", "bob", PER_MINUTE, BURST)
    assert not result.allowed
    assert 0 < result.retry_after <= T + 0.05


async def test_allowed_again_after_waiting(redis_client):
    for _ in range(BURST + 1):
        await check(redis_client, "query", "carol", PER_MINUTE, BURST)
    denied = await check(redis_client, "query", "carol", PER_MINUTE, BURST)
    assert not denied.allowed
    await asyncio.sleep(denied.retry_after + 0.02)
    result = await check(redis_client, "query", "carol", PER_MINUTE, BURST)
    assert result.allowed


async def test_users_and_classes_are_independent(redis_client):
    for _ in range(BURST + 2):
        await check(redis_client, "query", "dave", PER_MINUTE, BURST)
    # other user unaffected
    assert (await check(redis_client, "query", "erin", PER_MINUTE, BURST)).allowed
    # other class unaffected
    assert (await check(redis_client, "submit", "dave", PER_MINUTE, BURST)).allowed


async def test_idle_key_expires(redis_client):
    await check(redis_client, "query", "frank", PER_MINUTE, BURST)
    ttl = await redis_client.ttl(b"rl:query:frank")
    assert 0 < ttl <= math.ceil(BURST * T + T) + 1
