"""Wall-time stages shared by the submit worker and poller."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from math import ceil
from typing import Any

from app.clock import utcnow
from app.services.events import ch_datetime
from app.telemetry import child_span


class StageTimer:
    def __init__(self) -> None:
        self._totals: dict[str, tuple[int, float, float]] = {}
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
            count, total, maximum = self._totals.get(path, (0, 0.0, 0.0))
            self._totals[path] = (count + 1, total + elapsed, max(maximum, elapsed))
            self._samples.setdefault(path, []).append(elapsed)
            self._stack.reset(token)

    def p95(self) -> dict[str, float]:
        """An exact interval percentile for the stage report."""
        return {
            name: sorted(samples)[ceil(0.95 * len(samples)) - 1]
            for name, samples in self._samples.items()
        }

    def flush(self) -> dict[str, tuple[int, float, float]]:
        totals = self._totals
        self._totals = {}
        self._samples = {}
        return totals


def stage_metric_rows(
    worker: str, totals: dict[str, tuple[int, float, float]]
) -> list[dict[str, Any]]:
    stamp = ch_datetime(utcnow())
    return [
        {
            "ts": stamp,
            "metric": "worker_stage_seconds",
            "labels": {"worker": worker, "stage": stage},
            "value": total,
        }
        for stage, (_, total, _) in sorted(totals.items())
    ]
