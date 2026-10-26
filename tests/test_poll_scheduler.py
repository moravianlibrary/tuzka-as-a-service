"""The poller's sliding status scheduler: refill, bound, cancellation, failed checks.

Pure asyncio (no Redis/DB): ``drain_poll_queue`` takes the check/process functions, so
these pin the scheduling behavior without the worker's I/O.
"""

import asyncio
from collections.abc import Callable

import pytest

from app.schemas.redis import EngineRun
from app.workers.poller.steps import drain_poll_queue
from app.workers.poller.types import PollOutcome
from tests.factories import job_state


def _outcome(job_id: str, status: str = "running") -> PollOutcome:
    return PollOutcome(
        job_id=job_id,
        status=status,
        state=job_state(),
        run=EngineRun(),
        retryable=False,
        http_status=None,
        error=None,
    )


async def _wait_until(predicate: Callable[[], bool], timeout: float = 1.0) -> None:
    async def poll() -> None:
        while not predicate():
            await asyncio.sleep(0.001)

    await asyncio.wait_for(poll(), timeout)


async def test_a_finished_check_refills_its_slot_immediately() -> None:
    started: list[str] = []
    release = {job_id: asyncio.Event() for job_id in ("a", "b", "c", "d")}
    processed: list[str] = []
    active = 0
    max_active = 0

    async def check(job_id: str) -> PollOutcome:
        nonlocal active, max_active
        started.append(job_id)
        active += 1
        max_active = max(max_active, active)
        try:
            await release[job_id].wait()
            return _outcome(job_id, "done")
        finally:
            active -= 1

    async def process(outcomes: list[PollOutcome]) -> None:
        processed.extend(outcome.job_id for outcome in outcomes)

    task = asyncio.create_task(
        drain_poll_queue(["a", "b", "c", "d"], concurrency=2, check=check, process=process)
    )
    try:
        await _wait_until(lambda: len(started) == 2)
        assert started == ["a", "b"]

        release["a"].set()
        # 'c' starts as soon as 'a' frees a slot, while 'b' is still blocked.
        await _wait_until(lambda: "c" in started)
        assert "d" not in started
        assert max_active == 2

        release["b"].set()
        await _wait_until(lambda: "d" in started)
        release["c"].set()
        release["d"].set()
        await asyncio.wait_for(task, timeout=1)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    assert started == ["a", "b", "c", "d"]
    assert sorted(processed) == ["a", "b", "c", "d"]


async def test_the_window_never_exceeds_its_bound_and_checks_each_job_once() -> None:
    jobs = [str(index) for index in range(12)]
    active = 0
    max_active = 0
    processed: list[str] = []

    async def check(job_id: str) -> PollOutcome:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        try:
            await asyncio.sleep(0.005)
            return _outcome(job_id)
        finally:
            active -= 1

    async def process(outcomes: list[PollOutcome]) -> None:
        processed.extend(outcome.job_id for outcome in outcomes)

    await drain_poll_queue(jobs, concurrency=3, check=check, process=process)

    assert max_active == 3
    assert sorted(processed) == sorted(jobs)


async def test_cancelling_the_drain_cancels_and_awaits_every_active_check() -> None:
    started: list[str] = []
    cancelled: list[str] = []
    both_started = asyncio.Event()

    async def check(job_id: str) -> PollOutcome:
        started.append(job_id)
        if len(started) == 2:
            both_started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.append(job_id)
            raise

    async def process(_outcomes: list[PollOutcome]) -> None:
        raise AssertionError("nothing should finish before cancellation")

    task = asyncio.create_task(
        drain_poll_queue(["a", "b", "c"], concurrency=2, check=check, process=process)
    )
    await asyncio.wait_for(both_started.wait(), timeout=1)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sorted(started) == ["a", "b"]
    assert sorted(cancelled) == ["a", "b"]


async def test_a_failed_status_request_does_not_stop_the_other_checks() -> None:
    processed: list[PollOutcome] = []

    async def check(job_id: str) -> PollOutcome:
        if job_id == "unreachable":
            return PollOutcome(
                job_id=job_id,
                status="unreachable",
                state=job_state(),
                run=EngineRun(),
                retryable=True,
                http_status=None,
                error="ConnectError()",
            )
        return _outcome(job_id, "done")

    async def process(outcomes: list[PollOutcome]) -> None:
        processed.extend(outcomes)

    await drain_poll_queue(
        ["unreachable", "a", "b", "c"], concurrency=2, check=check, process=process
    )

    assert sorted(outcome.job_id for outcome in processed) == ["a", "b", "c", "unreachable"]
    failure = next(outcome for outcome in processed if outcome.job_id == "unreachable")
    assert failure.status == "unreachable"
    assert failure.retryable is True


async def test_an_unexpected_status_error_does_not_cancel_its_peers(caplog) -> None:
    processed: list[str] = []

    async def check(job_id: str) -> PollOutcome:
        if job_id == "boom":
            raise RuntimeError("status check bug")
        return _outcome(job_id, "done")

    async def process(outcomes: list[PollOutcome]) -> None:
        processed.extend(outcome.job_id for outcome in outcomes)

    await drain_poll_queue(["boom", "a", "b"], concurrency=2, check=check, process=process)

    assert sorted(processed) == ["a", "b"]
    assert "Status check failed unexpectedly" in caplog.text
