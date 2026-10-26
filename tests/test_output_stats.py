"""Pins the page statistics a harvested job's done event carries."""

from app.schemas.redis import EngineRun
from app.workers.poller.steps import output_stats
from app.workers.poller.types import OutputStats


def test_stats_come_from_the_engine_metrics_and_the_outputs() -> None:
    run = EngineRun(metrics={"n_lines": 3, "mean_conf": 1})

    stats = output_stats(run, (2, 40), b"ab c\n")

    assert stats == OutputStats(lines=3, blocks=2, chars=3, mean_conf=1.0)  # txt wins


def test_an_alto_only_job_counts_alto_characters() -> None:
    assert output_stats(EngineRun(), (2, 40), None).chars == 40


def test_missing_or_malformed_metrics_are_left_out() -> None:
    run = EngineRun(metrics={"n_lines": "many", "mean_conf": "high"})

    assert output_stats(run, None, None) == OutputStats(None, None, None, None)
