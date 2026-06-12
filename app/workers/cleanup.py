import asyncio
import logging
from datetime import datetime, timedelta

import redis.asyncio as aioredis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.models.backend import Backend  # noqa: F401 — registers FK target with mapper
from app.models.job_daily_stats import JobDailyStats  # noqa: F401 — registers table
from app.services.config import get_storage_ttl_minutes
from app.services.reaper import reap_stale_jobs
from app.services.stats import STATS_COLUMNS, daily_aggregation_select
from app.services.storage import (
    delete_objects,
    get_incoming_client,
    get_results_client,
    list_expired_objects,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("cleanup-worker")

# Raw jobs are kept this many days; older days are rolled up into job_daily_stats
# (kept forever) and then deleted. Hardcoded — not operator-tunable.
RETENTION_DAYS = 30

# Fixed key so concurrent cleanup runs can't double-process a day's rollup.
ROLLUP_ADVISORY_LOCK = 992_005

_ROLLUP_INSERT = (
    f"INSERT INTO job_daily_stats ({', '.join(STATS_COLUMNS)})\n"
    f"{daily_aggregation_select('finished_at < :cutoff')}\n"
    "ON CONFLICT (stat_date, username, engine_version, domain) DO NOTHING"
)


async def rollup_and_delete(db) -> None:
    """Roll whole days that are fully older than ``RETENTION_DAYS`` into
    ``job_daily_stats``, then delete those raw rows — all in one advisory-locked
    transaction so a re-run (or a second replica) re-derives identical numbers and
    the ON CONFLICT makes it a safe no-op.

    The cutoff is the midnight boundary 30 days back, so only days that have aged out
    *in their entirety* are processed; each such day is therefore complete and gets
    rolled up exactly once, which is what makes the stored percentiles exact."""
    cutoff = (datetime.utcnow() - timedelta(days=RETENTION_DAYS)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": ROLLUP_ADVISORY_LOCK})
    await db.execute(text(_ROLLUP_INSERT), {"cutoff": cutoff})
    await db.execute(
        text(
            "DELETE FROM job_results WHERE job_id IN "
            "(SELECT id FROM jobs WHERE finished_at < :cutoff)"
        ),
        {"cutoff": cutoff},
    )
    result = await db.execute(
        text("DELETE FROM jobs WHERE finished_at < :cutoff"), {"cutoff": cutoff}
    )
    await db.commit()
    if result.rowcount:
        logger.info(f"Rolled up + deleted {result.rowcount} job records older than {cutoff:%Y-%m-%d}")


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

                    await rollup_and_delete(db)
            except Exception as e:
                logger.error(f"Cleanup worker error: {e}")

        tick += 1
        await asyncio.sleep(REAP_TICK_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
