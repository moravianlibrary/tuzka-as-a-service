"""The workers' graceful stop: SIGTERM sets the event their loops check between passes."""

import asyncio
import os
import signal
import time

import pytest

from app.workers.shutdown import install_stop, sleep_unless_stopped


@pytest.mark.asyncio
async def test_sigterm_sets_the_stop_event() -> None:
    stop = install_stop()
    loop = asyncio.get_running_loop()
    try:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), 2)
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)
    assert stop.is_set()


@pytest.mark.asyncio
async def test_sleep_ends_early_on_stop() -> None:
    stop = asyncio.Event()
    asyncio.get_running_loop().call_later(0.05, stop.set)
    started = time.monotonic()
    await sleep_unless_stopped(stop, 30)
    assert time.monotonic() - started < 5


@pytest.mark.asyncio
async def test_sleep_runs_its_course_without_stop() -> None:
    stop = asyncio.Event()
    started = time.monotonic()
    await sleep_unless_stopped(stop, 0.05)
    assert time.monotonic() - started >= 0.04
    assert not stop.is_set()
