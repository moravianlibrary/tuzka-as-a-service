"""Tests for the pure batch rules (step 7): which members become pages, page order."""

import pytest

from app.services.batches import PAGE_SCORE_STEP, page_score, plan_batch_member

ALLOWED = [".tif", ".tiff", ".jpg", ".jpeg", ".png"]


@pytest.mark.parametrize(
    ("name", "plan"),
    [
        ("p1.jpg", "take"),
        ("scans/vol1/p2.TIFF", "take"),
        ("README.txt", "skip"),
        ("manifest", "skip"),
        ("pages/.hidden.png", "take"),
        ("photo.jpg.zip", "skip"),
    ],
)
def test_plan_batch_member(name, plan) -> None:
    assert plan_batch_member(name, ALLOWED) == plan


def test_pages_keep_archive_order_and_stay_together() -> None:
    scores = [page_score(1000.0, i) for i in range(2000)]
    assert scores == sorted(scores)
    # A whole 2000-page batch still sorts before a job submitted a millisecond later.
    assert scores[-1] < 1000.0 + 0.001
    assert scores[1] - scores[0] == pytest.approx(PAGE_SCORE_STEP)
