"""Pins the reaper's stale-job selection: queued/running jobs past their deadline get flagged."""

from datetime import datetime, timedelta

from app.services.reaper import select_stale_jobs


class _Job:
    def __init__(self, status, submitted_at, dispatched_at=None, domain=None):
        self.status = status
        self.submitted_at = submitted_at
        self.dispatched_at = dispatched_at
        self.domain = domain


def test_select_stale_jobs_flags_queued_and_running_past_deadline():
    now = datetime(2026, 6, 11, 12, 0, 0)
    jobs = [
        _Job("queued", submitted_at=now - timedelta(seconds=1000)),  # stale queued
        _Job("queued", submitted_at=now - timedelta(seconds=100)),  # fresh queued
        _Job(
            "running",
            submitted_at=now - timedelta(seconds=5000),
            dispatched_at=now - timedelta(seconds=400),
        ),  # stale running
        _Job(
            "running",
            submitted_at=now - timedelta(seconds=5000),
            dispatched_at=now - timedelta(seconds=100),
        ),  # fresh running
        _Job(
            "running", submitted_at=now - timedelta(seconds=5000), dispatched_at=None
        ),  # not yet dispatched -> skip
    ]
    stale = select_stale_jobs(
        jobs, now=now, queued_timeout=900, running_timeout_for=lambda domain: 300
    )
    assert [jobs.index(j) for j, _ in stale] == [0, 2]
    reasons = [reason for _, reason in stale]
    assert "queue" in reasons[0]
    assert "processing" in reasons[1]


def test_running_timeout_is_per_domain():
    now = datetime(2026, 6, 11, 12, 0, 0)
    ten_minutes_ago = now - timedelta(seconds=600)
    jobs = [
        _Job("running", now - timedelta(hours=1), ten_minutes_ago, domain=None),
        _Job("running", now - timedelta(hours=1), ten_minutes_ago, domain="maps"),
    ]
    timeouts = {"maps": 1800}

    stale = select_stale_jobs(
        jobs, now=now, queued_timeout=900, running_timeout_for=lambda d: timeouts.get(d, 300)
    )

    assert [jobs.index(j) for j, _ in stale] == [0]  # the map sheet still has time
    assert "300s" in stale[0][1]
