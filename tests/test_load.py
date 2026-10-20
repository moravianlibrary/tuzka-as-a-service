"""Tests for the pure load arithmetic behind GET /load (step 4)."""

import pytest

from app.services.load import ewma


def test_ewma_first_sample_is_taken_as_is() -> None:
    assert ewma(None, 12.0) == 12.0


def test_ewma_moves_a_tenth_towards_the_sample() -> None:
    assert ewma(10.0, 20.0) == pytest.approx(11.0)
    assert ewma(10.0, 20.0, alpha=0.5) == pytest.approx(15.0)
