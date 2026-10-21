"""Cleanup worker: reaps stale jobs, applies retention (job TTLs, results eviction) and
purges expired storage objects."""

import asyncio
import contextlib
import logging
from datetime import timedelta

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clock import utcnow
from app.config import Settings
from app.models.backend import Backend  # noqa: F401 — registers FK target with mapper
from app.models.db import make_engine
from app.services import batch_store, live_metrics
from app.services.config import get_storage_ttl_minutes
from app.services.event_emitter import get_emitter
from app.services.reaper import reap_stale_jobs
from app.services.redis_jobs import rebuild_load_counters
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

logger = logging.getLogger("cleanup-worker")

# Max objects per remove_objects call when purging expired storage objects.
DELETE_BATCH_SIZE = 1000


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = Settings()
    db_engine = make_engine(settings)
    setup_telemetry("taas-cleanup", db_engine=db_engine)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    incoming_client = get_incoming_client(settings)
    results_client = get_results_client(settings)
    r = aioredis.from_url(settings.redis_url, decode_responses=False)
    emitter = get_emitter(settings)  # the reaper's failed events
    await emitter.start()

    bucket_clients = {
        settings.minio_incoming_bucket: incoming_client,
        settings.minio_results_bucket: results_client,
    }

    # Queue depth and backend use for HyperDX, every 15 s (step 5, D6).
    live = (
        asyncio.create_task(live_metrics.run(r, session_factory, settings))
        if settings.clickhouse_url
        else None
    )

    logger.info("Cleanup worker started")
    REAP_TICK_SECONDS = 60
    HEAVY_EVERY = 10  # heavy object/retention sweep every 10th tick (~10 min)
    tick = 0
    try:
        while True:
            try:
                async with session_factory() as db:
                    await reap_stale_jobs(db, r)
            except Exception:
                logger.exception("Reaper failed")

            # Retention every tick: job rows past their TTL, results past their age or
            # over the storage cap (each bounded per tick).
            try:
                async with session_factory() as db:
                    bucket = settings.minio_results_bucket
                    await delete_finished_jobs(db, results_client, bucket)
                    await delete_expired_batches(db, results_client, bucket)
                    await evict_results(db, results_client, bucket)
                    if stalled := await batch_store.finish_stalled(db):
                        logger.warning(f"Finished {stalled} stalled batch(es)")
            except Exception:
                logger.exception("Retention sweep failed")

            if tick % HEAVY_EVERY == 0:
                try:
                    async with session_factory() as db:
                        ttls = await get_storage_ttl_minutes(db, list(bucket_clients.keys()))
                        for bucket, ttl_minutes in ttls.items():
                            client = bucket_clients[bucket]
                            cutoff = utcnow() - timedelta(minutes=ttl_minutes)
                            expired = await list_expired_objects(client, bucket, cutoff)
                            if expired:
                                for i in range(0, len(expired), DELETE_BATCH_SIZE):
                                    batch = expired[i : i + DELETE_BATCH_SIZE]
                                    await delete_objects(client, bucket, batch)
                                    logger.info(
                                        f"Deleted {len(batch)} expired objects from {bucket}"
                                    )

                except Exception:
                    logger.exception("Cleanup worker failed")
                try:
                    await rebuild_load_counters(r)
                except Exception:
                    logger.exception("Load counter rebuild failed")

            tick += 1
            await asyncio.sleep(REAP_TICK_SECONDS)
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
