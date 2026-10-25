"""Reaper: fail jobs that have been stuck in a non-terminal state too long.

The DB is the source of truth for job state, but only Redis-driven workers advance
jobs. A job can therefore stall forever — orphaned `queued` rows never in the Redis
queue, or `running` rows whose Redis state was lost. This sweep is the backstop: it
reads DB state directly, marks stale jobs failed, releases their Redis/backend slot,
and emits the WS failed event.
"""

import asyncio
import logging
from collections.abc import Callable, Coroutine, Iterable
from datetime import datetime, timedelta
from typing import Any, cast

import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.models.backend import Backend
from app.models.job import Job
from app.services import batch_store
from app.services import config as config_service
from app.services.event_emitter import get_emitter
from app.services.events import JobEvent, durations, failure
from app.services.redis_jobs import (
    batch_event,
    job_event_payload,
    pending_harvest_members,
    publish_event,
    set_failed,
)
from app.vocabulary import ACTIVE_JOB_STATUSES, FailureStage, JobEventName, JobStatus

logger = logging.getLogger("reaper")

# Cap how many stale jobs one sweep reaps; the rest are picked up on later ticks.
REAP_MAX_PER_TICK = 500
# Cap concurrent Redis set_failed / publish_event calls within a sweep.
_REAP_CONCURRENCY = 10


def select_stale_jobs(
    jobs: Iterable[Job],
    now: datetime,
    queued_timeout: int,
    running_timeout_for: Callable[[str | None], int],
) -> list[tuple[Job, str]]:
    """Return [(job, reason), ...] for jobs past their phase deadline.

    Pure function (no I/O) so it is unit-testable. `queued` is measured from
    `submitted_at`, `running` from `dispatched_at` (set when the job was handed to
    the engine) — each phase has its own clock. A job dispatched but not yet
    started has `dispatched_at` set, so it is still covered. The running timeout is
    per domain (a map sheet runs far longer than a text page), via
    `running_timeout_for(job.domain)`.
    """
    queued_cutoff = now - timedelta(seconds=queued_timeout)
    stale: list[tuple[Job, str]] = []
    for job in jobs:
        if job.status == JobStatus.QUEUED and job.submitted_at < queued_cutoff:
            stale.append((job, f"timed out in queue after {queued_timeout}s"))
        elif job.status in (JobStatus.RUNNING, JobStatus.STORING) and job.dispatched_at is not None:
            running_timeout = running_timeout_for(job.domain)
            if job.dispatched_at < now - timedelta(seconds=running_timeout):
                stale.append((job, f"timed out while processing after {running_timeout}s"))
    return stale


async def reap_stale_jobs(db: AsyncSession, r: aioredis.Redis) -> int:
    """Find stale jobs, fail them, release Redis/backend state, emit events.

    Returns the number of jobs reaped.
    """
    queued_timeout = await config_service.get_job_queued_timeout_seconds(db)
    running_timeout = await config_service.get_job_running_timeout_seconds(db)
    domain_limits = await config_service.get_domain_limits(db)
    state_ttl = await config_service.get_state_ttl_seconds(db)
    now = utcnow()

    result = await db.execute(
        select(Job).where(Job.status.in_(ACTIVE_JOB_STATUSES)).limit(REAP_MAX_PER_TICK)
    )
    candidates = result.scalars().all()

    def running_timeout_for(domain: str | None) -> int:
        limits = domain_limits.get(domain or "default")
        if limits is None or limits.running_timeout_seconds is None:
            return running_timeout
        return limits.running_timeout_seconds

    stale = select_stale_jobs(candidates, now, queued_timeout, running_timeout_for)
    if stale:
        # A job whose engine finished but whose harvest is still queued is not stuck:
        # its slot was released and the poller is working through the backlog. Failing
        # it here would throw away results that are already on the engine.
        harvesting = await pending_harvest_members(r, [str(job.id) for job, _ in stale])
        stale = [(job, reason) for job, reason in stale if str(job.id) not in harvesting]

    backend_ids = {job.backend_id for job, _ in stale if job.backend_id is not None}
    backend_labels: dict[int, str] = {}
    if backend_ids:
        rows = await db.execute(
            select(Backend.id, Backend.label).where(Backend.id.in_(backend_ids))
        )
        backend_labels = {backend_id: label or str(backend_id) for backend_id, label in rows}

    sem = asyncio.Semaphore(_REAP_CONCURRENCY)

    async def _bounded(coro: Coroutine[Any, Any, Any]) -> Any:
        async with sem:
            return await coro

    # set_failed releases the inflight slot (srem + decr backend counter). These calls are
    # independent per job, so run them concurrently (bounded) before the single DB commit.
    batches = await asyncio.gather(
        *(_bounded(set_failed(r, str(job.id), reason, state_ttl)) for job, reason in stale)
    )
    for job, reason in stale:
        job.status = JobStatus.FAILED
        job.error = reason
        job.finished_at = now
    if stale:
        for batch_id in {job.batch_id for job, _ in stale if job.batch_id}:
            await batch_store.finish_if_done(db, batch_id)
        await db.commit()
        await asyncio.gather(
            *(
                _bounded(
                    publish_event(
                        r,
                        job.username,
                        job_event_payload(
                            str(job.id),
                            job.external_id,
                            JobStatus.FAILED,
                            batch_id=str(job.batch_id) if job.batch_id else None,
                            error=reason,
                        ),
                    )
                )
                for job, reason in stale
            ),
            *(
                _bounded(publish_event(r, job.username, batch_event(batch)))
                for (job, _), batch in zip(stale, batches, strict=True)
                if batch is not None
            ),
        )
        emitter = get_emitter()
        for job, _reason in stale:
            emitter.emit(
                JobEvent(
                    ts=now,
                    event=JobEventName.FAILED,
                    job_id=str(job.id),
                    username=job.username,
                    domain=job.domain or "default",
                    outputs=sorted(job.outputs or []),
                    backend_id=job.backend_id,
                    backend=backend_labels.get(job.backend_id or 0, ""),
                    engine_version=job.engine_version or "",
                    file_size_bytes=job.file_size_bytes,
                    **failure(FailureStage.TIMEOUT, None),
                    **cast(
                        "dict[str, Any]",
                        durations(
                            job.submitted_at,
                            job.dispatched_at,
                            job.engine_received_at,
                            job.started_at,
                            now,
                            None,
                        ),
                    ),
                )
            )
        logger.info(f"Reaped {len(stale)} stale job(s)")
    return len(stale)
