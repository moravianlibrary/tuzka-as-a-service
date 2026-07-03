"""Tests for the event-driven submit wakeup.

signal_submit pushes a token the submit worker blocks on; the paths that make a job
dispatchable (enqueue) or free a backend slot (set_done/set_failed) must signal, and
wait_for_submit_signal must wake on a token and coalesce a burst. Needs redis (the
redis_client fixture skips if unreachable).
"""

from app.services import redis_jobs
from app.services.redis_jobs import (
    _SUBMIT_WAKEUP_KEY,
    signal_submit,
    wait_for_submit_signal,
)


async def _token_count(r) -> int:
    return await r.llen(_SUBMIT_WAKEUP_KEY)


async def test_signal_submit_pushes_a_token(redis_client) -> None:
    await signal_submit(redis_client)

    assert await _token_count(redis_client) == 1  # one wakeup token enqueued
    assert 0 < await redis_client.ttl(_SUBMIT_WAKEUP_KEY) <= 60  # TTL bounds stale tokens


async def test_enqueue_signals_the_worker(redis_client) -> None:
    await redis_jobs.enqueue_job(
        redis_client,
        "job-1",
        {"priority": "0", "submitted_at": "1.0", "username": "alice", "external_id": "x"},
        state_ttl=60,
    )

    assert await _token_count(redis_client) >= 1  # newly pending work woke the worker


async def test_set_done_signals_on_freed_slot(redis_client) -> None:
    await redis_jobs.set_running(redis_client, "job-2", "eng-1", "http://b", 7, state_ttl=60)
    await redis_client.delete(_SUBMIT_WAKEUP_KEY)  # ignore any earlier tokens

    await redis_jobs.set_done(redis_client, "job-2", state_ttl=60)

    assert await _token_count(redis_client) == 1  # freeing the backend slot woke the worker


async def test_wait_wakes_and_coalesces_burst(redis_client) -> None:
    for _ in range(5):
        await signal_submit(redis_client)

    await wait_for_submit_signal(redis_client, timeout=0.5)

    assert await _token_count(redis_client) == 0  # a burst drains to a single dispatch pass
