from datetime import datetime

from app.workers.poller import _parse_engine_dt


def test_parse_engine_dt_naive_utc_from_offset():
    # Engine emits tz-aware UTC ISO; taas stores naive UTC.
    dt = _parse_engine_dt("2026-06-13T10:00:00+00:00")
    assert dt == datetime(2026, 6, 13, 10, 0, 0)
    assert dt.tzinfo is None


def test_parse_engine_dt_handles_missing_and_invalid():
    assert _parse_engine_dt(None) is None
    assert _parse_engine_dt("") is None
    assert _parse_engine_dt("not-a-date") is None
