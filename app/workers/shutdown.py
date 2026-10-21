"""Graceful stop for the workers: SIGTERM lets the current pass finish, then they exit."""

import asyncio
import contextlib
import logging
import signal

logger = logging.getLogger(__name__)


def install_stop() -> asyncio.Event:
    """An event set on SIGTERM or SIGINT, for the worker loop to check between passes.

    The workers run as PID 1 in their container, where a signal with no handler is
    dropped: without this, Kubernetes' stop did nothing and the pod was SIGKILLed after
    the grace period, at whatever point of a pass it had reached."""
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()

    def request(sig: signal.Signals) -> None:
        logger.info("%s received; stopping after the current pass", sig.name)
        stop.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, request, sig)
    return stop


async def sleep_unless_stopped(stop: asyncio.Event, seconds: float) -> None:
    """Sleep ``seconds``, or less if a stop is requested meanwhile."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)
