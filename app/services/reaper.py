"""Reaper: fail jobs that have been stuck in a non-terminal state too long.

The DB is the source of truth for job state, but only Redis-driven workers advance
jobs. A job can therefore stall forever — orphaned `queued` rows never in the Redis
queue, or `running` rows whose Redis state was lost. This sweep is the backstop: it
reads DB state directly, marks stale jobs failed, releases their Redis/backend slot,
and emits the WS failed event.
"""

import logging
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.job import Job
from app.services import config as config_service
from app.services.redis_jobs import publish_event, set_failed

logger = logging.getLogger("reaper")


def select_stale_jobs(jobs, now, queued_timeout, running_timeout):
    """Return [(job, reason), ...] for jobs past their phase deadline.

    Pure function (no I/O) so it is unit-testable. `queued` is measured from
    `submitted_at`, `running` from `started_at` — each phase has its own clock.
    """
    queued_cutoff = now - timedelta(seconds=queued_timeout)
    running_cutoff = now - timedelta(seconds=running_timeout)
    stale = []
    for job in jobs:
        if job.status == "queued" and job.submitted_at < queued_cutoff:
            stale.append((job, f"timed out in queue after {queued_timeout}s"))
        elif (
            job.status == "running"
            and job.started_at is not None
            and job.started_at < running_cutoff
        ):
            stale.append((job, f"timed out while processing after {running_timeout}s"))
    return stale


async def reap_stale_jobs(db: AsyncSession, r) -> int:
    """Find stale jobs, fail them, release Redis/backend state, emit events.

    Returns the number of jobs reaped.
    """
    queued_timeout = await config_service.get_job_queued_timeout_seconds(db)
    running_timeout = await config_service.get_job_running_timeout_seconds(db)
    state_ttl = await config_service.get_state_ttl_seconds(db)
    now = datetime.utcnow()

    result = await db.execute(select(Job).where(Job.status.in_(("queued", "running"))))
    candidates = result.scalars().all()
    stale = select_stale_jobs(candidates, now, queued_timeout, running_timeout)

    for job, reason in stale:
        # set_failed releases the inflight slot (srem + decr backend counter).
        await set_failed(r, str(job.id), reason, state_ttl)
        job.status = "failed"
        job.error = reason
        job.finished_at = now
    if stale:
        await db.commit()
        for job, reason in stale:
            await publish_event(
                r,
                job.username,
                {"status": "failed", "uuid": str(job.external_id), "error": reason},
            )
        logger.info(f"Reaped {len(stale)} stale job(s)")
    return len(stale)
