"""Pins the poller's poll-result classification: harvest, fail-fast, requeue within budget, keep polling."""

import dataclasses

import httpx
import pytest

from app.engines.tuzkaocr import TuzkaOcrAdapter
from app.services.retry_policy import is_retryable_engine_failure, is_retryable_poll_error
from app.workers.poller import (
    PollOutcome,
    _probe_failure_status,
    classify_poll_result,
    poll_attempt,
    poll_failure_message,
)


def _http_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "http://engine/api/v1/status/x")
    return httpx.HTTPStatusError("err", request=request, response=httpx.Response(code))


def _outcome(**kw) -> PollOutcome:
    base = {
        "job_id": "j",
        "status": "failed",
        "meta": {"backend_id": "7"},
        "times": {},
        "retryable": False,
        "http_status": None,
        "error": None,
    }
    return PollOutcome(**(base | kw))


def test_done_harvests():
    assert classify_poll_result("done", False, requeues=0, max_requeues=3) == "harvest"


def test_running_keeps_polling():
    assert classify_poll_result("running", False, 0, 3) == "running"
    assert classify_poll_result("unknown", False, 0, 3) == "running"


def test_engine_422_fails_at_once():
    retryable = is_retryable_engine_failure(422)
    assert classify_poll_result("failed", retryable, 0, 3) == "fail"


def test_engine_500_requeues():
    retryable = is_retryable_engine_failure(500)
    assert classify_poll_result("failed", retryable, 0, 3) == "requeue"
    assert classify_poll_result("error", retryable, 2, 3) == "requeue"


def test_engine_failure_without_status_requeues():
    # D3: neither the status field nor the result probe told us anything → retry.
    retryable = is_retryable_engine_failure(None)
    assert classify_poll_result("failed", retryable, 0, 3) == "requeue"


def test_budget_exhausted_fails():
    assert classify_poll_result("failed", True, 3, 3) == "fail"
    assert classify_poll_result("unreachable", True, 3, 3) == "fail"


def test_unreachable_requeues_within_budget():
    assert classify_poll_result("unreachable", True, 0, 3) == "requeue"
    assert classify_poll_result("unreachable", True, 2, 3) == "requeue"


def test_poll_404_requeues():
    # The engine restarted and lost the job: retry it elsewhere (D1).
    retryable = is_retryable_poll_error(_http_error(404))
    assert classify_poll_result("error", retryable, 0, 3) == "requeue"


def test_poll_401_fails():
    retryable = is_retryable_poll_error(_http_error(401))
    assert classify_poll_result("error", retryable, 0, 3) == "fail"


def test_polloutcome_is_frozen():
    with pytest.raises(dataclasses.FrozenInstanceError):
        _outcome().status = "done"  # type: ignore[misc]


def test_unreachable_message_keeps_budget_wording():
    outcome = _outcome(status="unreachable", retryable=True, error="ConnectError()")
    assert poll_failure_message(outcome, 5) == "engine unreachable, exceeded 5 requeue attempts"


def test_engine_failure_message_is_engine_error_text():
    assert poll_failure_message(_outcome(error="bad image"), 5) == "bad image"
    assert poll_failure_message(_outcome(), 5) == "Engine error"


def test_poll_attempt_stage_and_backend():
    engine = poll_attempt(
        _outcome(http_status=500, error="crash", stage="engine"), True, "2026-06-01T12:00:00"
    )
    assert engine == {
        "at": "2026-06-01T12:00:00",
        "backend_id": 7,
        "error": "crash",
        "http_status": 500,
        "retry": True,
        "stage": "engine",
    }
    poll = poll_attempt(_outcome(status="unreachable", meta={}), False, "t")
    assert poll["stage"] == "poll"
    assert poll["backend_id"] is None
    assert poll["error"] == "unreachable"


def _make_adapter(handler) -> tuple[TuzkaOcrAdapter, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = TuzkaOcrAdapter(url="http://engine", api_key=None, http=http)
    return adapter, http


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [422, 500])
async def test_probe_failure_status_reads_result_http_status(code: int):
    # Older engines raise the failed job's error_status as the result endpoint's status.
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/result/e1"
        return httpx.Response(code, json={"detail": "Processing failed"})

    adapter, http = _make_adapter(handler)
    try:
        assert await _probe_failure_status(adapter, "e1") == code
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_probe_failure_status_none_when_unreachable():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    adapter, http = _make_adapter(handler)
    try:
        assert await _probe_failure_status(adapter, "e1") is None
    finally:
        await http.aclose()
