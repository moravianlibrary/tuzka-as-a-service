"""Wall-time stages shared by the submit worker and poller."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from math import ceil
from typing import Any

from app.clock import utcnow
from app.services.events import ch_datetime
from app.telemetry import child_span


@dataclass(frozen=True)
class StageTotals:
    """One stage's runs since the last flush: how many, and their total and longest time."""

    count: int = 0
    total_s: float = 0.0
    max_s: float = 0.0

    def added(self, elapsed: float) -> StageTotals:
        return StageTotals(self.count + 1, self.total_s + elapsed, max(self.max_s, elapsed))


class StageTimer:
    def __init__(self) -> None:
        self._totals: dict[str, StageTotals] = {}
        self._samples: dict[str, list[float]] = {}
        self._stack: ContextVar[tuple[str, ...]] = ContextVar("stage_stack", default=())

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        stack = self._stack.get()
        path = ".".join((*stack, name))
        token = self._stack.set((*stack, name))
        start = time.perf_counter()
        try:
            with child_span(name):
                yield
        finally:
            elapsed = time.perf_counter() - start
            self._totals[path] = self._totals.get(path, StageTotals()).added(elapsed)
            self._samples.setdefault(path, []).append(elapsed)
            self._stack.reset(token)

    def p95(self) -> dict[str, float]:
        """An exact interval percentile for the stage report."""
        return {
            name: sorted(samples)[ceil(0.95 * len(samples)) - 1]
            for name, samples in self._samples.items()
        }

    def flush(self) -> dict[str, StageTotals]:
        totals = self._totals
        self._totals = {}
        self._samples = {}
        return totals


def log_stages(
    logger: logging.Logger, totals: dict[str, StageTotals], p95: dict[str, float]
) -> None:
    for stage, t in sorted(totals.items()):
        logger.info(
            "stage=%s n=%d total_s=%.6f max_s=%.6f p95_s=%.6f",
            stage,
            t.count,
            t.total_s,
            t.max_s,
            p95[stage],
        )


def stage_metric_rows(worker: str, totals: dict[str, StageTotals]) -> list[dict[str, Any]]:
    stamp = ch_datetime(utcnow())
    return [
        {
            "ts": stamp,
            "metric": "worker_stage_seconds",
            "labels": {"worker": worker, "stage": stage},
            "value": t.total_s,
        }
        for stage, t in sorted(totals.items())
    ]
