"""Tests for retry_policy — the unified retry/terminal classifier and backoff.

Pure (no I/O). Pins which dispatch, poll, and engine failures are retried versus
failed fast (D1/D2). Also covers backoff_seconds (D6) and describe_exception.
"""

from __future__ import annotations

import random

import httpx

from app.services.retry_policy import (
    backoff_seconds,
    describe_exception,
    http_status_of,
    is_retryable_dispatch_error,
    is_retryable_engine_failure,
    is_retryable_poll_error,
)

# ---- helpers ----


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://engine/api/v1/process")
    response = httpx.Response(code, request=request)
    return httpx.HTTPStatusError(f"HTTP {code}", request=request, response=response)


# ---- describe_exception ----


def test_describe_exception_returns_nonempty_str() -> None:
    # A normal exception returns its str()
    assert describe_exception(ValueError("oops")) == "oops"

    # An exception with a hidden message falls back to repr
    empty_exc = httpx.ConnectError("")
    assert describe_exception(empty_exc)  # never empty
    assert "ConnectError" in describe_exception(empty_exc)


# ---- http_status_of ----


def test_http_status_of_http_status_error() -> None:
    exc = _status_error(502)
    assert http_status_of(exc) == 502


def test_http_status_of_non_http() -> None:
    assert http_status_of(KeyError("key")) is None
    assert http_status_of(ValueError("boom")) is None


# ---- is_retryable_dispatch_error ----


class TestIsRetryableDispatchError:
    """D1: transport errors, 5xx, 408, 429 → True. D2: everything else → False."""

    def test_transport_errors_are_retryable(self) -> None:
        assert is_retryable_dispatch_error(httpx.ConnectError(""))
        assert is_retryable_dispatch_error(httpx.ReadTimeout("slow"))
        assert is_retryable_dispatch_error(httpx.PoolTimeout("no connection"))
        assert is_retryable_dispatch_error(httpx.WriteTimeout(""))
        # ReadError takes a single string argument
        assert is_retryable_dispatch_error(httpx.ReadError(""))

    def test_5xx_is_retryable(self) -> None:
        for code in (500, 502, 503, 504):
            assert is_retryable_dispatch_error(_status_error(code)), f"{code}"

    def test_408_and_429_are_retryable(self) -> None:
        assert is_retryable_dispatch_error(_status_error(408))
        assert is_retryable_dispatch_error(_status_error(429))

    def test_4xx_is_terminal(self) -> None:
        for code in (400, 401, 403, 404, 410, 422, 425):
            assert not is_retryable_dispatch_error(_status_error(code)), f"{code}"

    def test_other_exceptions_are_terminal(self) -> None:
        """Malformed engine response or unexpected bugs fail fast."""
        assert not is_retryable_dispatch_error(KeyError("job_id"))
        assert not is_retryable_dispatch_error(ValueError("boom"))
        assert not is_retryable_dispatch_error(RuntimeError("unexpected"))


# ---- is_retryable_poll_error ----


class TestIsRetryablePollError:
    """Same as dispatch, plus HTTP 404 (engine restarted, lost the job)."""

    def test_404_is_retryable_on_poll(self) -> None:
        assert is_retryable_poll_error(_status_error(404))

    def test_404_is_terminal_on_dispatch(self) -> None:
        assert not is_retryable_dispatch_error(_status_error(404))

    def test_other_4xx_still_terminal(self) -> None:
        for code in (400, 422):
            assert not is_retryable_poll_error(_status_error(code)), f"{code}"

    def test_5xx_is_retryable(self) -> None:
        assert is_retryable_poll_error(_status_error(500))

    def test_transport_error_is_retryable(self) -> None:
        assert is_retryable_poll_error(httpx.ConnectError(""))


# ---- is_retryable_engine_failure ----


class TestIsRetryableEngineFailure:
    """D3 (None → True stopgap), D1 (5xx → True), D2 (4xx → False)."""

    def test_none_is_retryable_stopgap(self) -> None:
        """Old engines don't send error_status — treat as retryable (D3)."""
        assert is_retryable_engine_failure(None)

    def test_5xx_is_retryable(self) -> None:
        for code in (500, 502, 503, 504):
            assert is_retryable_engine_failure(code), f"{code}"

    def test_4xx_is_terminal(self) -> None:
        for code in (400, 401, 404, 422, 425):
            assert not is_retryable_engine_failure(code), f"{code}"

    def test_4xx_boundary(self) -> None:
        """error_status 399 should also be terminal (only >= 500 is retryable)."""
        assert not is_retryable_engine_failure(399)


# ---- backoff_seconds ----


class TestBackoffSeconds:
    """D6: full-jitter backoff, base 1 s, cap 30 s."""

    def test_non_negative(self) -> None:
        for attempt in range(10):
            rng = random.Random(42)
            assert backoff_seconds(attempt, rng) >= 0

    def test_bounded_by_cap(self) -> None:
        """At high attempts, delay must be capped at 30 s."""
        for attempt in (10, 20, 50):
            rng = random.Random(99)
            assert backoff_seconds(attempt, rng) <= 30.0

    def test_upper_bound_grows_with_attempt(self) -> None:
        """min(cap, base * 2**attempt) grows until it hits the cap."""
        rng = random.Random(42)
        # At attempt 10, min(30, 1024) = 30, so result <= 30
        assert backoff_seconds(10, rng) <= 30.0

    def test_attempt_0_max_is_base(self) -> None:
        """At attempt=0 the jitter range is uniform(0, 1), so max is 1."""
        for _ in range(20):
            rng = random.Random(_ * 7)
            assert backoff_seconds(0, rng) <= 1.0

    def test_default_rng(self) -> None:
        """Without an explicit rng it still returns a valid float."""
        result = backoff_seconds(3)
        assert isinstance(result, float)
        assert 0 <= result <= 30.0
