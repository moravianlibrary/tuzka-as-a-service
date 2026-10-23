"""Dispatch must account for work on engines and results awaiting storage."""

from app.workers.submit import unstored_capacity


def test_unstored_capacity_counts_both_phases() -> None:
    assert unstored_capacity(100, 60, 30) == 10
    assert unstored_capacity(100, 80, 30) == 0
    assert unstored_capacity(100, 0, 0) == 100
