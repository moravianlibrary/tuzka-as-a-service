"""Stage timing captures wall time, nested stages and reset behavior."""

import asyncio

import pytest

from app.workers.stages import StageTimer, stage_metric_rows


def test_counts_totals_and_flush_reset() -> None:
    timer = StageTimer()
    with timer.stage("peek"):
        pass
    with timer.stage("peek"):
        pass
    count, total, maximum = timer.flush()["peek"]
    assert count == 2
    assert total >= maximum >= 0
    assert timer.flush() == {}


def test_p95_resets_with_flush() -> None:
    timer = StageTimer()
    with timer.stage("poll"):
        pass
    assert 0 <= timer.p95()["poll"] <= timer.flush()["poll"][2]
    assert timer.p95() == {}


def test_nested_stages() -> None:
    timer = StageTimer()
    with timer.stage("dispatch"), timer.stage("engine_submit"):
        pass
    totals = timer.flush()
    assert set(totals) == {"dispatch", "dispatch.engine_submit"}
    assert totals["dispatch"][1] >= totals["dispatch.engine_submit"][1]


@pytest.mark.asyncio
async def test_stage_includes_await_time() -> None:
    timer = StageTimer()
    with timer.stage("wait"):
        await asyncio.sleep(0.02)
    assert timer.flush()["wait"][1] >= 0.015


def test_stage_metric_labels() -> None:
    rows = stage_metric_rows("poller", {"poll": (2, 1.25, 0.8)})
    assert len(rows) == 1
    assert rows[0]["metric"] == "worker_stage_seconds"
    assert rows[0]["labels"] == {"worker": "poller", "stage": "poll"}
    assert rows[0]["value"] == 1.25
