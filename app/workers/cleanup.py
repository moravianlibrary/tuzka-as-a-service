"""Cleanup worker: reaps stale jobs, applies retention (job TTLs, results eviction) and
purges expired storage objects."""

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import timedelta

import redis.asyncio as aioredis
from miniopy_async.api import Minio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import utcnow
from app.config import Settings
from app.models.backend import Backend  # noqa: F401 — registers FK target with mapper
from app.models.db import make_engine, make_session_factory
from app.models.job import Job
from app.services import batch_store, live_metrics
from app.services.config import get_runtime_config
from app.services.event_emitter import get_emitter
from app.services.reaper import reap_stale_jobs
from app.services.redis_jobs import (
    rebuild_backend_counters,
    rebuild_load_counters,
    requeue_orphans,
)
from app.services.redis_pool import make_redis
from app.services.retention import (
    delete_expired_batches,
    delete_finished_jobs,
    evict_results,
)
from app.services.storage import (
    delete_objects,
    get_incoming_client,
    get_results_client,
    list_expired_objects,
)
from app.telemetry import setup_telemetry
from app.vocabulary import JobStatus
from app.workers.shutdown import install_stop, sleep_unless_stopped

logger = logging.getLogger("cleanup-worker")

# Max objects per remove_objects call when purging expired storage objects.
DELETE_BATCH_SIZE = 1000
# Queued jobs checked for a lost queue entry per tick, and how old they must be.
ORPHAN_SCAN_LIMIT = 10_000
ORPHAN_MIN_AGE = timedelta(seconds=60)


async def requeue_lost_jobs(db: AsyncSession, r: aioredis.Redis) -> int:
    """Put back on their queue the queued jobs that are in none (a submit worker claimed
    them and died before dispatching). Without this they'd sit until the queued timeout
    fails them, and /load wouldn't count them, so KEDA wouldn't scale for them."""
    cutoff = utcnow() - ORPHAN_MIN_AGE
    ids = (
        await db.scalars(
            select(Job.id)
            .where(Job.status == JobStatus.QUEUED, Job.submitted_at < cutoff)
            .limit(ORPHAN_SCAN_LIMIT)
        )
    ).all()
    requeued = await requeue_orphans(r, [str(job_id) for job_id in ids], time.time())
    if requeued:
        logger.warning("Requeued %d job(s) missing from the queue: %s", len(requeued), requeued)
    return len(requeued)


# How often the cleanup runs, and how often of those the heavy object/retention sweep
# does (every 10th tick, ~10 min).
REAP_TICK_SECONDS = 60
HEAVY_EVERY = 10


class CleanupWorker:
    """The cleanup worker's clients, and the steps of a tick."""

    def __init__(
        self,
        settings: Settings,
        r: aioredis.Redis,
        session_factory: async_sessionmaker[AsyncSession],
        incoming_client: Minio,
        results_client: Minio,
        stop: asyncio.Event,
    ) -> None:
        self.settings = settings
        self.r = r
        self.session_factory = session_factory
        self.results_client = results_client
        self.bucket_clients = {
            settings.minio_incoming_bucket: incoming_client,
            settings.minio_results_bucket: results_client,
        }
        self.stop = stop
        self.ticks = 0

    async def run(self) -> None:
        logger.info("Cleanup worker started")
        while not self.stop.is_set():
            await self.run_tick()
            await sleep_unless_stopped(self.stop, REAP_TICK_SECONDS)

    async def run_tick(self) -> None:
        """Every tick: requeue lost jobs, reap stale ones, apply retention. Every
        ``HEAVY_EVERY``-th: also purge expired objects and rebuild the counters. A step
        that fails is logged and the rest still run."""
        is_heavy = self.ticks % HEAVY_EVERY == 0
        self.ticks += 1
        await _guard("Requeue of lost jobs failed", self._requeue_lost_jobs)
        await _guard("Reaper failed", self._reap)
        await _guard("Retention sweep failed", self._apply_retention)
        if not is_heavy:
            return

        await _guard("Purging expired objects failed", self._purge_expired_objects)
        await _guard("Load counter rebuild failed", lambda: rebuild_load_counters(self.r))
        await _guard("Backend counter rebuild failed", self._rebuild_backend_counters)

    async def _requeue_lost_jobs(self) -> None:
        async with self.session_factory() as db:
            await requeue_lost_jobs(db, self.r)

    async def _reap(self) -> None:
        async with self.session_factory() as db:
            await reap_stale_jobs(db, self.r)

    async def _apply_retention(self) -> None:
        """Job rows past their TTL, results past their age or over the storage cap (each
        bounded per tick), and batches whose last page's finish was missed."""
        bucket = self.settings.minio_results_bucket
        async with self.session_factory() as db:
            await delete_finished_jobs(db, self.results_client, bucket)
            await delete_expired_batches(db, self.results_client, bucket)
            await evict_results(db, self.results_client, bucket)
            if stalled := await batch_store.finish_stalled(db):
                logger.warning(f"Finished {stalled} stalled batch(es)")

    async def _purge_expired_objects(self) -> None:
        """Delete each bucket's objects older than its ``storage.<bucket>_ttl_minutes``."""
        async with self.session_factory() as db:
            config = await get_runtime_config(db)
        for bucket, client in self.bucket_clients.items():
            cutoff = utcnow() - timedelta(minutes=config.storage_ttl_minutes(bucket))
            expired = await list_expired_objects(client, bucket, cutoff)
            for i in range(0, len(expired), DELETE_BATCH_SIZE):
                batch = expired[i : i + DELETE_BATCH_SIZE]
                await delete_objects(client, bucket, batch)
                logger.info(f"Deleted {len(batch)} expired objects from {bucket}")

    async def _rebuild_backend_counters(self) -> None:
        if fixed := await rebuild_backend_counters(self.r):
            logger.warning(f"Corrected the in-flight count of {fixed} backend(s)")


async def _guard(failure: str, step: Callable[[], Awaitable[object]]) -> None:
    """Run one cleanup step; a failure is logged, so it can't stop the steps after it
    or end the worker."""
    try:
        await step()
    except Exception:
        logger.exception(failure)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    db_engine = make_engine(settings)
    setup_telemetry(settings, "taas-cleanup", db_engine=db_engine)
    session_factory = make_session_factory(db_engine)
    r = make_redis(settings)
    emitter = get_emitter(settings)  # the reaper's failed events
    await emitter.start()
    worker = CleanupWorker(
        settings,
        r,
        session_factory,
        get_incoming_client(settings),
        get_results_client(settings),
        install_stop(),
    )

    # Queue depth and backend use for HyperDX, every 15 s (step 5, D6).
    live = (
        asyncio.create_task(live_metrics.run(r, session_factory, settings))
        if settings.clickhouse_url
        else None
    )
    try:
        await worker.run()
    finally:
        if live is not None:
            live.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await live
        await emitter.aclose()
        await r.aclose()
        await db_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
