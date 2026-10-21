"""Retention (step 6, D5): job rows by TTL, result data by age and a storage cap.

Postgres keeps live state, not history (that's ClickHouse), so finished job rows go
after their TTL: done ones soon, failed ones later, for debugging. Result objects can be
downloaded more than once until they expire by age, or until the results storage cap
evicts the oldest first. The decisions are pure; the two async functions do the I/O.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from miniopy_async.api import Minio
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.clock import utcnow
from app.engines.outputs import ext_for
from app.models.batch import Batch
from app.models.job import Job, JobResult
from app.services import config as config_service
from app.services.batch_archive import batch_archive_path
from app.services.storage import delete_objects

logger = logging.getLogger(__name__)

# Rows per delete round, and rounds per cleanup tick: the first deploy after 0.9 deletes
# millions of old rows, ~20 000 a minute, without one huge transaction.
DELETE_BATCH = 1000
MAX_BATCHES_PER_TICK = 20
# The cap evicts down to this fraction of it, so it isn't hit again on the next result.
EVICT_TO = 0.9


def expired_job_cutoffs(
    now: datetime, done_ttl_s: int, failed_ttl_s: int
) -> tuple[datetime, datetime]:
    """Finished-before times past which done / failed job rows are deleted."""
    return now - timedelta(seconds=done_ttl_s), now - timedelta(seconds=failed_ttl_s)


def plan_eviction(
    oldest_first: Sequence[tuple[Any, int]], used_bytes: int, cap_bytes: int
) -> list[Any]:
    """Which results to evict, oldest first, so usage drops under ``EVICT_TO`` of the cap.
    ``oldest_first`` is ``(key, size_bytes)``; nothing goes while usage is within the cap."""
    if used_bytes <= cap_bytes:
        return []
    target = cap_bytes * EVICT_TO
    evict: list[Any] = []
    for key, size in oldest_first:
        if used_bytes <= target:
            break
        evict.append(key)
        used_bytes -= size
    return evict


def _object(username: str, job_id: uuid.UUID, fmt: str) -> str:
    return f"{username}/{job_id}.{ext_for(fmt)}.zst"


async def delete_finished_jobs(db: AsyncSession, results_client: Minio, bucket: str) -> int:
    """Delete single jobs past their TTL, with their result rows and objects. Returns how
    many jobs went."""
    done_ttl = await config_service.get_done_ttl_seconds(db)
    failed_ttl = await config_service.get_failed_ttl_seconds(db)
    done_cutoff, failed_cutoff = expired_job_cutoffs(utcnow(), done_ttl, failed_ttl)
    # Pages of a batch go with their batch (delete_expired_batches), all at once.
    expired = (
        ((Job.status == "done") & (Job.finished_at < done_cutoff))
        | ((Job.status == "failed") & (Job.finished_at < failed_cutoff))
    ) & Job.batch_id.is_(None)

    deleted = 0
    for _ in range(MAX_BATCHES_PER_TICK):
        jobs = (
            await db.execute(select(Job.id, Job.username).where(expired).limit(DELETE_BATCH))
        ).all()
        if not jobs:
            break
        ids = [job_id for job_id, _ in jobs]
        owner: dict[uuid.UUID, str] = {job_id: username for job_id, username in jobs}
        results = (
            await db.execute(
                select(JobResult.job_id, JobResult.fmt).where(
                    JobResult.job_id.in_(ids), JobResult.evicted_at.is_(None)
                )
            )
        ).all()
        if results:
            paths = [_object(owner[job_id], job_id, fmt) for job_id, fmt in results]
            await delete_objects(results_client, bucket, paths)
        await db.execute(delete(JobResult).where(JobResult.job_id.in_(ids)))
        await db.execute(delete(Job).where(Job.id.in_(ids)))
        await db.commit()
        deleted += len(ids)
    if deleted:
        logger.info("Deleted %d finished job(s) past their TTL", deleted)
    return deleted


def batch_expired(
    finished_at: datetime, any_failed: bool, now: datetime, done_ttl_s: int, failed_ttl_s: int
) -> bool:
    """A finished batch goes after the done TTL, or the failed TTL if any page failed, so
    its failures stay around as long as a single failed job's would (step 7, D7)."""
    ttl = failed_ttl_s if any_failed else done_ttl_s
    return finished_at < now - timedelta(seconds=ttl)


async def delete_expired_batches(db: AsyncSession, results_client: Minio, bucket: str) -> int:
    """Delete finished batches past their TTL: their pages' result objects, their results
    archives, then the batch (its jobs cascade). Returns how many batches went."""
    done_ttl = await config_service.get_done_ttl_seconds(db)
    failed_ttl = await config_service.get_failed_ttl_seconds(db)
    now = utcnow()
    any_failed = (
        select(func.count())
        .select_from(Job)
        .where(Job.batch_id == Batch.id, Job.status == "failed")
        .correlate(Batch)
        .scalar_subquery()
        > 0
    )
    finished = (
        await db.execute(
            select(Batch.id, Batch.username, Batch.finished_at, any_failed)
            .where(
                Batch.status == "finished",
                Batch.finished_at < now - timedelta(seconds=min(done_ttl, failed_ttl)),
            )
            .order_by(Batch.finished_at)
            .limit(DELETE_BATCH)
        )
    ).all()
    expired = [
        (batch_id, username)
        for batch_id, username, finished_at, failed in finished
        if batch_expired(finished_at, bool(failed), now, done_ttl, failed_ttl)
    ]
    for batch_id, username in expired:
        results = (
            await db.execute(
                select(JobResult.job_id, JobResult.fmt)
                .join(Job, Job.id == JobResult.job_id)
                .where(Job.batch_id == batch_id, JobResult.evicted_at.is_(None))
            )
        ).all()
        paths = [_object(username, job_id, fmt) for job_id, fmt in results]
        paths += [
            batch_archive_path(username, batch_id),
            batch_archive_path(username, batch_id, partial=True),
        ]
        for i in range(0, len(paths), DELETE_BATCH):
            await delete_objects(results_client, bucket, paths[i : i + DELETE_BATCH])
        page_ids = select(Job.id).where(Job.batch_id == batch_id)
        await db.execute(delete(JobResult).where(JobResult.job_id.in_(page_ids)))
        await db.execute(delete(Batch).where(Batch.id == batch_id))
        await db.commit()
    if expired:
        logger.info("Deleted %d finished batch(es) past their TTL", len(expired))
    return len(expired)


async def evict_results(db: AsyncSession, results_client: Minio, bucket: str) -> int:
    """Delete result objects past the max age, then the oldest ones while the results
    stored exceed the cap. Their rows are kept (marked evicted) until the job's TTL, so
    a download answers 410 instead of 404. Returns how many were evicted.

    A batch's results archive is a copy of its pages' results, so it counts toward the
    cap and ages out with them. It goes first when over the cap: it's only a cache, and
    the next download rebuilds it from the page results that are still there."""
    now = utcnow()
    max_age_cutoff = now - timedelta(seconds=await config_service.get_results_max_age_seconds(db))
    limit = DELETE_BATCH * MAX_BATCHES_PER_TICK
    live = JobResult.evicted_at.is_(None)
    rows = select(JobResult.job_id, JobResult.fmt, Job.username, JobResult.size_bytes).join(
        Job, Job.id == JobResult.job_id
    )
    built = Batch.archive_built_at.is_not(None)
    archives = select(Batch.id, Batch.username, Batch.archive_size_bytes)

    aged = (await db.execute(rows.where(live, Job.stored_at < max_age_cutoff).limit(limit))).all()
    aged_archives = (
        await db.execute(
            archives.where(built, Batch.archive_built_at < max_age_cutoff).limit(limit)
        )
    ).all()
    evict = [(job_id, fmt, user) for job_id, fmt, user, _ in aged]
    evict_archives = [(batch_id, user) for batch_id, user, _ in aged_archives]

    cap = await config_service.get_results_max_bytes(db)
    if cap is not None:
        stored = select(func.coalesce(func.sum(JobResult.size_bytes), 0)).where(live)
        stored_archives = select(func.coalesce(func.sum(Batch.archive_size_bytes), 0)).where(built)
        used = int(await db.scalar(stored) or 0) + int(await db.scalar(stored_archives) or 0)
        used -= sum(size or 0 for *_, size in aged)
        used -= sum(size or 0 for *_, size in aged_archives)
        if used > cap:
            newer_archives = (
                await db.execute(
                    archives.where(built, Batch.archive_built_at >= max_age_cutoff)
                    .order_by(Batch.archive_built_at)
                    .limit(limit)
                )
            ).all()
            oldest = (
                await db.execute(
                    rows.where(live, Job.stored_at >= max_age_cutoff)
                    .order_by(Job.stored_at)
                    .limit(limit)
                )
            ).all()
            candidates: list[tuple[Any, int]] = [
                (("archive", batch_id, user), size or 0) for batch_id, user, size in newer_archives
            ] + [(("result", job_id, fmt, user), size or 0) for job_id, fmt, user, size in oldest]
            for key in plan_eviction(candidates, used, cap):
                if key[0] == "archive":
                    evict_archives.append(key[1:])
                else:
                    evict.append(key[1:])

    if not evict and not evict_archives:
        return 0
    paths = [_object(user, job_id, fmt) for job_id, fmt, user in evict]
    paths += [batch_archive_path(user, batch_id) for batch_id, user in evict_archives]
    for i in range(0, len(paths), DELETE_BATCH):
        await delete_objects(results_client, bucket, paths[i : i + DELETE_BATCH])
    for job_id, fmt, _ in evict:
        await db.execute(
            update(JobResult)
            .where(JobResult.job_id == job_id, JobResult.fmt == fmt)
            .values(evicted_at=now, presigned_url=None)
        )
    if evict_archives:
        await db.execute(
            update(Batch)
            .where(Batch.id.in_([batch_id for batch_id, _ in evict_archives]))
            .values(archive_built_at=None, archive_size_bytes=None)
        )
    await db.commit()
    logger.info(
        "Evicted %d result object(s) and %d batch archive(s) (max age / storage cap)",
        len(evict),
        len(evict_archives),
    )
    return len(evict) + len(evict_archives)
