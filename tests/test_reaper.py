from datetime import datetime, timedelta

from app.services.reaper import select_stale_jobs


class _Job:
    def __init__(self, status, submitted_at, started_at=None):
        self.status = status
        self.submitted_at = submitted_at
        self.started_at = started_at


def test_select_stale_jobs_flags_queued_and_running_past_deadline():
    now = datetime(2026, 6, 11, 12, 0, 0)
    jobs = [
        _Job("queued", submitted_at=now - timedelta(seconds=1000)),   # stale queued
        _Job("queued", submitted_at=now - timedelta(seconds=100)),    # fresh queued
        _Job("running", submitted_at=now - timedelta(seconds=5000),
             started_at=now - timedelta(seconds=400)),                # stale running
        _Job("running", submitted_at=now - timedelta(seconds=5000),
             started_at=now - timedelta(seconds=100)),                # fresh running
    ]
    stale = select_stale_jobs(jobs, now=now, queued_timeout=900, running_timeout=300)
    assert [jobs.index(j) for j, _ in stale] == [0, 2]
    reasons = [reason for _, reason in stale]
    assert "queue" in reasons[0]
    assert "processing" in reasons[1]
