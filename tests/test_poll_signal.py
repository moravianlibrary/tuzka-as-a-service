"""Tests for the event-driven poller wakeup.

signal_poll pushes a token the poller blocks on; set_running (a job going in-flight)
must signal it, and wait_for_poll_signal must wake on a token, coalesce a burst, and
treat a non-positive timeout as 'don't wait' (BLPOP's 0 means block-forever). Needs
redis (the redis_client fixture skips if unreachable).
"""

from app.services import redis_jobs
from app.services.redis_jobs import (
    _POLL_WAKEUP_KEY,
    signal_poll,
    wait_for_poll_signal,
)


async def _token_count(r) -> int:
    return await r.llen(_POLL_WAKEUP_KEY)


async def test_signal_poll_pushes_a_token(redis_client) -> None:
    await signal_poll(redis_client)

    assert await _token_count(redis_client) == 1  # one wakeup token enqueued
    assert 0 < await redis_client.ttl(_POLL_WAKEUP_KEY) <= 60  # TTL bounds stale tokens


async def test_set_running_signals_the_poller(redis_client) -> None:
    await redis_client.delete(_POLL_WAKEUP_KEY)

    await redis_jobs.set_running(redis_client, "job-1", "eng-1", "http://b", 7, state_ttl=60)

    assert await _token_count(redis_client) >= 1  # a job going in-flight woke the poller


async def test_wait_wakes_and_coalesces_burst(redis_client) -> None:
    for _ in range(5):
        await signal_poll(redis_client)

    await wait_for_poll_signal(redis_client, timeout=0.5)

    assert await _token_count(redis_client) == 0  # a burst drains to a single poll pass


async def test_wait_returns_immediately_on_nonpositive_timeout(redis_client) -> None:
    # A past deadline yields timeout <= 0; must not block (BLPOP 0 blocks forever) and
    # must leave any pending token untouched for the next real wait.
    await redis_client.delete(_POLL_WAKEUP_KEY)
    await signal_poll(redis_client)

    await wait_for_poll_signal(redis_client, timeout=0.0)

    assert await _token_count(redis_client) == 1  # returned without consuming the token
