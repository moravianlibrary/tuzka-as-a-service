"""Tests for delayed retries in Redis: schedule_retry, promote_due_retries, parse_avoid.

Uses the ``redis_client`` fixture (skips if Redis not reachable).  Pins that
a scheduled job is not pending before its time, is promoted after it, and that
a second promote run is a no-op.  Also verifies avoid_backends deduplication.
"""

import time

import pytest
import redis.asyncio as aioredis

from app.services.redis_jobs import (
    _DELAYED_KEY,
    _PENDING_KEY,
    job_event_payload,
    parse_avoid,
    promote_due_retries,
    schedule_retry,
)

# ---- parse_avoid (pure) ----


def test_parse_avoid_empty() -> None:
    assert parse_avoid({}) == frozenset()
    assert parse_avoid({"avoid_backends": ""}) == frozenset()


def test_parse_avoid_single() -> None:
    assert parse_avoid({"avoid_backends": "5"}) == frozenset({5})


def test_parse_avoid_multiple() -> None:
    result = parse_avoid({"avoid_backends": "3,5,7"})
    assert result == frozenset({3, 5, 7})


def test_parse_avoid_deduplicates() -> None:
    result = parse_avoid({"avoid_backends": "3,5,3,7"})
    assert result == frozenset({3, 5, 7})


# ---- schedule_retry + promote_due_retries ----


@pytest.mark.asyncio
async def test_scheduled_job_not_pending_before_time(
    redis_client: aioredis.Redis,
) -> None:
    """A job scheduled for the future is NOT in the pending ZSET."""
    job_id = "test-scheduled-future"
    original_score = 100.0
    delay_s = 60.0  # 60 seconds in the future

    # Set up a minimal job hash so get_job returns a priority
    key = f"job:{job_id}"
    await redis_client.hset(key, mapping={"priority": "0", "status": "queued"})

    await schedule_retry(redis_client, job_id, original_score, delay_s, avoid_backend_id=1)

    # Job should be in delayed ZSET
    delayed_score = await redis_client.zscore(_DELAYED_KEY, job_id)
    assert delayed_score is not None

    # But NOT in the pending ZSET yet
    pending_key = _PENDING_KEY.format(priority=0)
    assert await redis_client.zscore(pending_key, job_id) is None


@pytest.mark.asyncio
async def test_promote_due_retries_promotes_job(
    redis_client: aioredis.Redis,
) -> None:
    """After delay passes, promote_due_retries moves the job to pending."""
    job_id = "test-promote-due"
    original_score = 200.0

    key = f"job:{job_id}"
    await redis_client.hset(key, mapping={"priority": "0", "status": "queued"})

    await schedule_retry(redis_client, job_id, original_score, delay_s=0.0, avoid_backend_id=2)

    now = time.time()
    promoted = await promote_due_retries(redis_client, now)
    assert promoted == 1

    # Job should now be in pending ZSET
    pending_key = _PENDING_KEY.format(priority=0)
    assert await redis_client.zscore(pending_key, job_id) == original_score

    # And removed from delayed ZSET
    assert await redis_client.zscore(_DELAYED_KEY, job_id) is None


@pytest.mark.asyncio
async def test_second_promote_due_retries_is_noop(
    redis_client: aioredis.Redis,
) -> None:
    """A second run of promote_due_retries returns 0 (idempotent)."""
    job_id = "test-second-promote"
    original_score = 300.0

    key = f"job:{job_id}"
    await redis_client.hset(key, mapping={"priority": "0", "status": "queued"})

    await schedule_retry(redis_client, job_id, original_score, delay_s=0.0, avoid_backend_id=3)

    now = time.time()
    assert await promote_due_retries(redis_client, now) == 1
    assert await promote_due_retries(redis_client, now) == 0  # no-op


@pytest.mark.asyncio
async def test_avoid_backends_accumulates_without_duplicates(
    redis_client: aioredis.Redis,
) -> None:
    """avoid_backends field accumulates backend ids without duplicates."""
    job_id = "test-avoid-dup"
    original_score = 400.0

    key = f"job:{job_id}"
    await redis_client.hset(key, mapping={"priority": "0", "status": "queued"})

    # Schedule with backend 1
    await schedule_retry(redis_client, job_id, original_score, delay_s=0.0, avoid_backend_id=1)

    # Schedule again with backend 1 (should deduplicate)
    await schedule_retry(redis_client, job_id, original_score, delay_s=0.0, avoid_backend_id=1)

    # Schedule with backend 2
    await schedule_retry(redis_client, job_id, original_score, delay_s=0.0, avoid_backend_id=2)

    avoid = await redis_client.hget(key, "avoid_backends")
    avoid_str = avoid.decode() if isinstance(avoid, bytes) else avoid
    ids = sorted(int(v) for v in avoid_str.split(",") if v.strip())
    assert ids == [1, 2]  # backend 1 deduplicated


def test_job_event_payload_shape() -> None:
    assert job_event_payload("j1", "uuid:abc", "failed", error="boom") == {
        "job_id": "j1",
        "external_id": "uuid:abc",
        "status": "failed",
        "error": "boom",
    }
    assert job_event_payload("j2", "", "done")["external_id"] is None
