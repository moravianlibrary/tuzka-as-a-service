"""Tests for the pure mapping of the ClickHouse backfill (step 5 T7)."""

import uuid
from datetime import datetime

from app.ops.backfill_clickstack import event_from_analytics, parse_cursor

JOB = uuid.UUID("0b8f1d7e-2b2c-4c5d-9a39-3a3e1b1d2c3f")


def _row(**overrides):
    row = {
        "job_id": JOB,
        "submitted_at": datetime(2026, 7, 1, 10, 0, 0),
        "status": "done",
        "fmt": "multi",
        "device": "gpu",
        "backend_id": 3,
        "file_size_bytes": 1234,
        "system_queue_s": 1.0,
        "engine_queue_s": 0.5,
        "ocr_running_s": 6.0,
        "time_in_system_s": 9.0,
        "alto_lines": 40,
        "alto_blocks": 5,
        "alto_chars": 900,
        "mean_conf": 0.93,
        "username": "alice",
        "domain": None,
        "engine_version": "1.7.0",
        "adapter": "tuzkaocr",
        "backend": "box1-gpu1",
    }
    row.update(overrides)
    return row


def test_done_row() -> None:
    event = event_from_analytics(_row())
    assert event.event == "done"
    assert event.ts == datetime(2026, 7, 1, 10, 0, 9)  # submitted + time in system
    assert event.job_id == str(JOB)
    assert event.domain == "default"
    assert event.outputs == ["alto", "txt"]
    assert (event.queue_s, event.run_s, event.total_s) == (1.0, 6.0, 9.0)
    assert event.backend == "box1-gpu1"
    assert event.error_flag is False


def test_failed_row_without_times() -> None:
    event = event_from_analytics(
        _row(status="failed", time_in_system_s=None, fmt="alto", adapter=None, username=None)
    )
    assert event.event == "failed"
    assert event.error_flag is True
    assert event.error_class == ""  # history has no error detail
    assert event.ts == datetime(2026, 7, 1, 10, 0, 0)
    assert event.outputs == ["alto"]
    assert (event.adapter, event.username) == ("", "")


def test_parse_cursor() -> None:
    assert parse_cursor(f"2026-07-01T10:00:00/{JOB}") == (datetime(2026, 7, 1, 10, 0), str(JOB))
