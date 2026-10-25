"""Tests for the pure job-event builders (step 5): error classes and the row shape."""

import json
from datetime import datetime

import pytest

from app.schemas.attempt import Attempt
from app.services.events import (
    JobEvent,
    attempt_failure,
    durations,
    error_class,
    extra_metrics,
    job_event_row,
)
from app.vocabulary import FailureStage


@pytest.mark.parametrize(
    ("stage", "http_status", "transport", "expected"),
    [
        ("dispatch", None, True, "transport"),
        ("poll", None, True, "transport"),
        ("dispatch", 503, False, "http_5xx"),
        ("poll", 404, False, "http_4xx"),
        ("harvest", 500, False, "http_5xx"),
        ("engine", 422, False, "engine_input"),
        ("engine", 500, False, "engine_internal"),
        ("engine", None, False, "engine_internal"),  # an old engine without error_status
        ("timeout", None, False, "timeout"),
        ("dispatch", None, False, ""),  # e.g. the image couldn't be read
    ],
)
def test_error_class(stage, http_status, transport, expected) -> None:
    assert error_class(stage, http_status, transport) == expected


def test_durations_stay_on_one_clock() -> None:
    t = datetime(2026, 10, 20, 12, 0, 0)
    spans = durations(
        submitted_at=t,
        dispatched_at=t.replace(second=2),
        engine_received_at=t.replace(second=1),  # engine clock, a little behind
        started_at=t.replace(second=4),
        finished_at=t.replace(second=10),
        stored_at=t.replace(second=11),
    )
    assert spans == {
        "queue_s": 2.0,
        "engine_queue_s": 3.0,
        "run_s": 6.0,
        "store_s": 1.0,
        "total_s": 11.0,
    }
    assert durations(t, None, None, None, None, None)["total_s"] is None


def test_job_event_row_round_trips_through_json() -> None:
    event = JobEvent(
        ts=datetime(2026, 10, 20, 12, 0, 0, 123456),
        event="failed",
        job_id="0b8f1d7e-2b2c-4c5d-9a39-3a3e1b1d2c3f",
        username="alice",
        domain="default",
        outputs=["alto", "txt"],
        backend_id=3,
        error_flag=True,
        error_class="http_5xx",
        http_status=502,
        total_s=1.5,
    )
    row = json.loads(json.dumps(job_event_row(event)))
    assert row["ts"] == "2026-10-20 12:00:00.123"
    assert row["error_flag"] == 1
    assert row["outputs"] == ["alto", "txt"]
    assert row["batch_id"] is None
    assert row["backend"] == ""
    assert row["extra"] == {}
    assert "error" not in row  # error text stays in Postgres


def test_job_event_row_carries_the_backend_label() -> None:
    event = JobEvent(
        ts=datetime(2026, 10, 20, 12, 0, 0),
        event="done",
        job_id="0b8f1d7e-2b2c-4c5d-9a39-3a3e1b1d2c3f",
        username="alice",
        domain="default",
        backend_id=3,
        backend="box1-gpu1",
    )
    assert job_event_row(event)["backend"] == "box1-gpu1"


def _attempt(stage: FailureStage, http_status: int | None) -> Attempt:
    return Attempt(
        at=datetime(2026, 6, 1),
        stage=stage,
        backend_id=1,
        http_status=http_status,
        error="x",
        retry=False,
    )


def test_attempt_failure_reads_the_attempt() -> None:
    poll_unreachable = _attempt(stage=FailureStage.POLL, http_status=None)
    assert attempt_failure(poll_unreachable)["error_class"] == "transport"
    engine_bad_input = _attempt(stage=FailureStage.ENGINE, http_status=422)
    assert attempt_failure(engine_bad_input) == {
        "error_flag": True,
        "error_class": "engine_input",
        "http_status": 422,
    }
    assert attempt_failure(None)["error_class"] == ""  # a harvest error


def test_extra_metrics_keeps_adapter_specific_ones_as_strings() -> None:
    metrics = {
        "n_lines": 12,
        "mean_conf": 0.9,
        "words": 340,
        "georef.status": "ok",
        "georef.residual_m": 4.2,
        "missing": None,
    }
    assert extra_metrics(metrics) == {
        "georef.residual_m": "4.2",
        "georef.status": "ok",
        "words": "340",
    }
