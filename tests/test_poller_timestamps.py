"""Pins engine-timestamp parsing: offsets normalize to naive UTC; missing/invalid inputs are tolerated."""

from datetime import datetime

from app.engines.base import EngineStatus
from app.schemas.redis import EngineRun
from app.workers.poller import _engine_run, _parse_engine_dt, engine_stamps


def test_parse_engine_dt_naive_utc_from_offset():
    # Engine emits tz-aware UTC ISO; taas stores naive UTC.
    dt = _parse_engine_dt("2026-06-13T10:00:00+00:00")
    assert dt == datetime(2026, 6, 13, 10, 0, 0)
    assert dt.tzinfo is None


def test_parse_engine_dt_converts_non_utc_offset_to_utc():
    # +02:00 12:00 -> 10:00 UTC, tz stripped.
    dt = _parse_engine_dt("2026-06-13T12:00:00+02:00")
    assert dt == datetime(2026, 6, 13, 10, 0, 0)
    assert dt.tzinfo is None


def test_parse_engine_dt_handles_missing_and_invalid():
    assert _parse_engine_dt(None) is None
    assert _parse_engine_dt("") is None
    assert _parse_engine_dt("not-a-date") is None


def test_engine_run_carries_engine_timestamps_and_metrics() -> None:
    status = EngineStatus(
        state="done",
        created_at="2026-01-01T00:00:00+00:00",
        started_at="2026-01-01T00:00:01+00:00",
        finished_at="2026-01-01T00:00:05+00:00",
        metrics={"n_lines": 12, "mean_conf": 0.9},
    )
    assert _engine_run(status) == EngineRun(
        created_at="2026-01-01T00:00:00+00:00",
        started_at="2026-01-01T00:00:01+00:00",
        finished_at="2026-01-01T00:00:05+00:00",
        metrics={"n_lines": 12, "mean_conf": 0.9},
    )


def test_engine_stamps_map_engine_times_to_job_columns() -> None:
    run = EngineRun(
        created_at="2026-01-01T00:00:00+00:00",
        started_at="2026-01-01T00:00:01+00:00",
        finished_at="2026-01-01T00:08:30+00:00",
        metrics={"n_words": 7},
    )
    assert engine_stamps(run) == {
        "engine_received_at": datetime(2026, 1, 1, 0, 0, 0),
        "started_at": datetime(2026, 1, 1, 0, 0, 1),
        "finished_at": datetime(2026, 1, 1, 0, 8, 30),
    }


def test_engine_stamps_skip_what_the_engine_did_not_report() -> None:
    # An unreachable engine gives no times: the failed job keeps taas's finished_at.
    assert engine_stamps(EngineRun()) == {}
    assert engine_stamps(EngineRun(started_at="2026-01-01T00:00:01Z")) == {
        "started_at": datetime(2026, 1, 1, 0, 0, 1)
    }
