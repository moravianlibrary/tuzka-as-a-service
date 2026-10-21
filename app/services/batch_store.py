"""Batch queries and the one batch state change the workers make (step 7)."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.backend import Backend
from app.models.backend_domain import BackendDomain
from app.models.domain import Domain
from app.models.job import Job, JobResult


def estimate_eta(remaining_pages: int, seconds_per_page: float | None, slots: int) -> float | None:
    """A rough ETA: the pages left times seconds per page, spread over the slots of the
    backends serving the domain. None without a seconds-per-page measurement."""
    if seconds_per_page is None:
        return None
    return remaining_pages * seconds_per_page / max(1, slots)


def _latest_pages(batch_id: uuid.UUID) -> Any:
    """The batch's current job per page: a retried page has several jobs, the newest
    one counts (step 7, D10)."""
    return (
        select(Job)
        .where(Job.batch_id == batch_id)
        .order_by(Job.source_name, Job.submitted_at.desc())
        .distinct(Job.source_name)
        .subquery()
    )


async def page_counts(db: AsyncSession, batch_id: uuid.UUID) -> dict[str, int]:
    """status → number of the batch's pages in it."""
    latest = _latest_pages(batch_id)
    rows = await db.execute(select(latest.c.status, func.count()).group_by(latest.c.status))
    return {status: count for status, count in rows.all()}


async def domain_slots(db: AsyncSession, domain: str) -> int:
    """How many pages of ``domain`` the enabled backends can run at once."""
    total = await db.scalar(
        select(func.coalesce(func.sum(Backend.max_inflight), 0))
        .join(BackendDomain, BackendDomain.backend_id == Backend.id)
        .join(Domain, Domain.id == BackendDomain.domain_id)
        .where(Domain.name == domain, Backend.enabled.is_(True))
    )
    return int(total or 0)


async def pages(db: AsyncSession, batch_id: uuid.UUID) -> list[Any]:
    """Every page (its current job), ordered by source name."""
    latest = _latest_pages(batch_id)
    rows = await db.execute(
        select(latest.c.id, latest.c.source_name, latest.c.status, latest.c.error).order_by(
            latest.c.source_name
        )
    )
    return list(rows.all())


async def stored_results(db: AsyncSession, batch_id: uuid.UUID) -> dict[uuid.UUID, list[str]]:
    """job id → the outputs stored for it (not evicted)."""
    rows = await db.execute(
        select(JobResult.job_id, JobResult.fmt)
        .join(Job, Job.id == JobResult.job_id)
        .where(Job.batch_id == batch_id, JobResult.evicted_at.is_(None))
    )
    stored: dict[uuid.UUID, list[str]] = {}
    for job_id, fmt in rows.all():
        stored.setdefault(job_id, []).append(fmt)
    return stored


_LOCK = text("SELECT 1 FROM batches WHERE id = :batch_id FOR UPDATE")

_FINISH = text(
    """
    UPDATE batches SET status = 'finished', finished_at = now()
    WHERE id = :batch_id AND status = 'queued'
      AND NOT EXISTS (
        SELECT 1 FROM jobs WHERE batch_id = :batch_id AND status IN ('queued', 'running')
      )
    """
)


async def finish_if_done(db: AsyncSession, batch_id: str | uuid.UUID | None) -> None:
    """Mark the batch finished once none of its pages is queued or running; a no-op for
    every page but the last. The caller commits.

    Two workers finishing the last two pages at once would each still see the other's
    page running (neither has committed) and neither would finish the batch. So the
    batch row is locked first: the second worker waits for the first to commit, and its
    check (a new snapshot under READ COMMITTED) sees both pages done."""
    if batch_id:
        await db.execute(_LOCK, {"batch_id": str(batch_id)})
        await db.execute(_FINISH, {"batch_id": str(batch_id)})


_FINISH_STALLED = text(
    """
    UPDATE batches b SET status = 'finished', finished_at = now()
    WHERE b.status = 'queued'
      AND NOT EXISTS (
        SELECT 1 FROM jobs j WHERE j.batch_id = b.id AND j.status IN ('queued', 'running')
      )
    """
)


async def finish_stalled(db: AsyncSession) -> int:
    """Safety net for the cleanup worker: finish every queued batch with no page left to
    run, in case its last page's finish was missed. Commits; returns how many."""
    result = await db.execute(_FINISH_STALLED)
    await db.commit()
    return int(getattr(result, "rowcount", 0) or 0)
