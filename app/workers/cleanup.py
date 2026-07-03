"""Cleanup worker: reaps stale jobs and purges expired storage objects and old job rows."""

import asyncio
import logging
from datetime import timedelta
from typing import Any, cast

import redis.asyncio as aioredis
from sqlalchemy import CursorResult, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.clock import utcnow
from app.config import Settings
from app.models.backend import Backend  # noqa: F401 — registers FK target with mapper
from app.services.config import get_storage_ttl_minutes
from app.services.reaper import reap_stale_jobs
from app.services.storage import (
    delete_objects,
    get_incoming_client,
    get_results_client,
    list_expired_objects,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cleanup-worker")

# Raw jobs are deleted after this many days; analytics rows in job_analytics are permanent.
RETENTION_DAYS = 30
# Max objects per remove_objects call when purging expired storage objects.
DELETE_BATCH_SIZE = 1000


async def delete_expired_jobs(db: AsyncSession) -> None:
    """Delete raw job rows (and their results) older than RETENTION_DAYS.

    job_analytics rows are permanent and are NOT deleted here."""
    cutoff = (utcnow() - timedelta(days=RETENTION_DAYS)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    await db.execute(
        text(
            "DELETE FROM job_results WHERE job_id IN "
            "(SELECT id FROM jobs WHERE finished_at < :cutoff)"
        ),
        {"cutoff": cutoff},
    )
    result = cast(
        "CursorResult[Any]",
        await db.execute(text("DELETE FROM jobs WHERE finished_at < :cutoff"), {"cutoff": cutoff}),
    )
    await db.commit()
    if result.rowcount:
        logger.info(f"Deleted {result.rowcount} raw job records older than {cutoff:%Y-%m-%d}")


async def main() -> None:
    settings = Settings()
    db_engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    incoming_client = get_incoming_client(settings)
    results_client = get_results_client(settings)
    r = aioredis.from_url(settings.redis_url, decode_responses=False)

    bucket_clients = {
        settings.minio_incoming_bucket: incoming_client,
        settings.minio_results_bucket: results_client,
    }

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

                        await delete_expired_jobs(db)
                except Exception:
                    logger.exception("Cleanup worker failed")

            tick += 1
            await asyncio.sleep(REAP_TICK_SECONDS)
    finally:
        await r.aclose()
        await db_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
