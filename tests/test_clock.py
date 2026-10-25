"""API timestamps: stored naive UTC, returned as UTC with a ``Z``."""

from datetime import UTC, datetime, timedelta, timezone

from app.clock import utc_iso
from app.schemas.job import JobStatusResponse


def _status(**times) -> JobStatusResponse:
    return JobStatusResponse(
        job_id="00000000-0000-0000-0000-000000000001",
        status="done",
        fmt="multi",
        domain=None,
        error=None,
        **{"started_at": None, "finished_at": None, **times},
    )


def test_naive_stored_time_is_returned_as_utc_with_z() -> None:
    body = _status(submitted_at=datetime(2026, 10, 21, 8, 2, 19, 668317)).model_dump(mode="json")
    assert body["submitted_at"] == "2026-10-21T08:02:19.668317Z"
    assert body["finished_at"] is None


def test_aware_time_is_converted_to_utc() -> None:
    cest = timezone(timedelta(hours=2))
    body = _status(
        submitted_at=datetime(2026, 10, 21, 10, 0, tzinfo=cest),
        finished_at=datetime(2026, 10, 21, 8, 0, tzinfo=UTC),
    ).model_dump(mode="json")
    assert body["submitted_at"] == "2026-10-21T08:00:00Z"
    assert body["finished_at"] == "2026-10-21T08:00:00Z"


def test_utc_iso() -> None:
    assert utc_iso(datetime(2026, 10, 21, 8, 2, 19)) == "2026-10-21T08:02:19Z"
