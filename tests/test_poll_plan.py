"""Pins how a chunk of poll outcomes is sorted, and a running job's poll backoff."""

from datetime import datetime

from app.schemas.redis import EngineRun
from app.vocabulary import PollStatus
from app.workers.poller.steps import next_poll_delay, plan_polls
from app.workers.poller.types import PollOutcome
from tests.factories import job_state

NOW = datetime(2026, 10, 26, 12)


def _outcome(job_id: str, status: PollStatus, retryable: bool = False, requeues: int = 0):
    state = job_state(backend_id=7, requeues=requeues)
    return PollOutcome(job_id, status, state, EngineRun(), retryable, None, "boom")


def test_each_outcome_lands_in_its_list() -> None:
    plan = plan_polls(
        [
            _outcome("done", PollStatus.DONE),
            _outcome("running", PollStatus.RUNNING),
            _outcome("failed", PollStatus.FAILED),
            _outcome("requeued", PollStatus.UNREACHABLE, retryable=True, requeues=2),
        ],
        5,
        NOW,
    )

    assert [o.job_id for o in plan.done] == ["done"]
    assert [o.job_id for o in plan.running] == ["running"]
    (failed,) = plan.failed
    assert (failed.outcome.job_id, failed.error, failed.attempt.retry) == ("failed", "boom", False)
    (requeued,) = plan.requeued
    assert requeued.outcome.job_id == "requeued"
    assert requeued.attempt.retry and requeued.attempt.at == NOW
    assert 0 <= requeued.delay_s <= 4.0  # full jitter over 1 s * 2**requeues


def test_a_running_job_backs_off_by_doubling_within_bounds() -> None:
    polled = job_state(last_poll=100.0, next_poll_at=104.0)

    assert next_poll_delay(polled, 104.0, 2.0, 30.0) == 8.0
    assert next_poll_delay(polled, 104.0, 2.0, 5.0) == 5.0  # capped
    assert next_poll_delay(job_state(), 104.0, 2.0, 30.0) == 2.0  # never polled: initial
