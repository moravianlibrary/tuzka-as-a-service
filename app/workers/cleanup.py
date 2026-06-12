import asyncio
import logging
from datetime import datetime, timedelta

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.models.backend import Backend  # noqa: F401 — registers FK target with mapper
from app.services import config as config_service
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
    while True:
        try:
            async with session_factory() as db:
                await reap_stale_jobs(db, r)
        except Exception as e:
            logger.error(f"Reaper error: {e}")

        if tick % HEAVY_EVERY == 0:
            try:
                async with session_factory() as db:
                    ttls = await get_storage_ttl_minutes(db, list(bucket_clients.keys()))
                    for bucket, ttl_minutes in ttls.items():
                        client = bucket_clients[bucket]
                        cutoff = datetime.utcnow() - timedelta(minutes=ttl_minutes)
                        expired = await list_expired_objects(client, bucket, cutoff)
                        if expired:
                            for i in range(0, len(expired), 1000):
                                batch = expired[i : i + 1000]
                                await delete_objects(client, bucket, batch)
                                logger.info(
                                    f"Deleted {len(batch)} expired objects from {bucket}"
                                )

                    # retention_days <= 0 disables row deletion (keep jobs forever).
                    # Guard required: a negative value would make the cutoff a future
                    # timestamp and delete every finished job.
                    retention_days = await config_service.get_job_retention_days(db)
                    if retention_days > 0:
                        cutoff_ret = datetime.utcnow() - timedelta(days=retention_days)

                        from sqlalchemy import text

                        await db.execute(
                            text(
                                "DELETE FROM job_results WHERE job_id IN "
                                "(SELECT id FROM jobs WHERE finished_at < :cutoff)"
                            ),
                            {"cutoff": cutoff_ret},
                        )
                        result = await db.execute(
                            text("DELETE FROM jobs WHERE finished_at < :cutoff"),
                            {"cutoff": cutoff_ret},
                        )
                        if result.rowcount:
                            logger.info(f"Cleaned up {result.rowcount} old job records")
                        await db.commit()
            except Exception as e:
                logger.error(f"Cleanup worker error: {e}")

        tick += 1
        await asyncio.sleep(REAP_TICK_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
