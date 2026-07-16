"""Tests for next_poll_timeout — the poller's event-driven wait computation.

Pure (no I/O). Pins the wake schedule: nearest deadline drives the wait, clamped to
[0, cap]. A past deadline means poll now (0, so wait_for_poll_signal returns at once);
nothing in flight waits the full cap (a dispatch signal wakes it sooner).
"""

import pytest

from app.workers.poller import next_poll_timeout


def test_no_deadlines_waits_the_cap() -> None:
    # Nothing in flight — block up to the idle ceiling (dispatch signal wakes sooner).
    assert next_poll_timeout([], now=100.0, cap=1.0) == 1.0


def test_waits_until_the_soonest_deadline() -> None:
    # Wake for the nearest, not the coarse grid: 0.3s away, not the 5.0s one.
    assert next_poll_timeout([100.3, 105.0], now=100.0, cap=1.0) == pytest.approx(0.3)


def test_past_deadline_yields_zero() -> None:
    # An overdue job means poll immediately; never a negative timeout.
    assert next_poll_timeout([99.0], now=100.0, cap=1.0) == 0.0


def test_far_deadline_is_capped() -> None:
    # A deadline beyond the cap still re-evaluates at the cap (self-heal ceiling).
    assert next_poll_timeout([108.0], now=100.0, cap=1.0) == 1.0


def test_deadline_exactly_at_cap() -> None:
    assert next_poll_timeout([101.0], now=100.0, cap=1.0) == 1.0
