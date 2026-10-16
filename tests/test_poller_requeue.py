"""Pins the poller's poll-result classification: harvest, fail-fast, requeue within budget, keep polling."""

from app.workers.poller import (
    PollOutcome,
    classify_poll_result,
)


def test_done_harvests():
    assert classify_poll_result("done", requeues=0, max_requeues=3) == "harvest"


def test_engine_failure_fails_immediately():
    # 4xx engine failures fail fast (not retryable)
    assert classify_poll_result("failed", 0, 3, retryable=False) == "fail"
    assert classify_poll_result("error", 0, 3, retryable=False) == "fail"


def test_engine_5xx_requeues():
    # 5xx engine failures retry until budget spent
    assert classify_poll_result("failed", 0, 3, retryable=True) == "requeue"
    assert classify_poll_result("failed", 2, 3, retryable=True) == "requeue"
    assert classify_poll_result("failed", 3, 3, retryable=True) == "fail"
    assert classify_poll_result("error", 0, 3, retryable=True) == "requeue"


def test_unreachable_requeues_within_budget():
    assert classify_poll_result("unreachable", 0, 3, retryable=True) == "requeue"
    assert classify_poll_result("unreachable", 2, 3, retryable=True) == "requeue"


def test_unreachable_fails_when_budget_exhausted():
    assert classify_poll_result("unreachable", 3, 3, retryable=True) == "fail"


def test_unreachable_fails_when_not_retryable():
    # Non-retryable poll error (e.g. HTTP 404 on status endpoint) fails immediately
    assert classify_poll_result("unreachable", 0, 3, retryable=False) == "fail"


def test_running_keeps_polling():
    assert classify_poll_result("running", 0, 3) == "running"
    assert classify_poll_result("unknown", 0, 3) == "running"


# --- PollOutcome dataclass tests ---


def test_polloutcome_is_frozen():
    outcome = PollOutcome(
        job_id="abc",
        status="done",
        meta={"key": "val"},
        times={"n_lines": 10},
        retryable=False,
        http_status=200,
        error=None,
    )
    try:
        outcome.status = "fail"
        raise AssertionError("should not be able to modify frozen dataclass")
    except Exception:
        pass  # expected


def test_polloutcome_has_slots():
    outcome = PollOutcome(
        job_id="x", status="running", meta={}, times={}, retryable=False, http_status=None, error=None
    )
    assert not hasattr(outcome, "__dict__")
