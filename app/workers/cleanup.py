import asyncio
import logging
from datetime import datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.services.config import get_storage_ttl_minutes
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

    bucket_clients = {
        settings.minio_incoming_bucket: incoming_client,
        settings.minio_results_bucket: results_client,
    }

    logger.info("Cleanup worker started")
    while True:
        try:
            async with session_factory() as db:
                ttls = await get_storage_ttl_minutes(db, list(bucket_clients.keys()))
                for bucket, ttl_minutes in ttls.items():
                    client = bucket_clients[bucket]
                    cutoff = datetime.utcnow() - timedelta(minutes=ttl_minutes)
                    expired = await list_expired_objects(client, bucket, cutoff)
                    if expired:
                        # Delete in batches of 1000
                        for i in range(0, len(expired), 1000):
                            batch = expired[i : i + 1000]
                            await delete_objects(client, bucket, batch)
                            logger.info(f"Deleted {len(batch)} expired objects from {bucket}")

                # Postgres cleanup: old job_results and jobs
                cutoff_90d = datetime.utcnow() - timedelta(days=90)

                from sqlalchemy import text

                await db.execute(
                    text(
                        "DELETE FROM job_results WHERE job_id IN "
                        "(SELECT id FROM jobs WHERE finished_at < :cutoff)"
                    ),
                    {"cutoff": cutoff_90d},
                )
                result = await db.execute(
                    text("DELETE FROM jobs WHERE finished_at < :cutoff"),
                    {"cutoff": cutoff_90d},
                )
                if result.rowcount:
                    logger.info(f"Cleaned up {result.rowcount} old job records")
                await db.commit()

        except Exception as e:
            logger.error(f"Cleanup worker error: {e}")

        await asyncio.sleep(600)  # every 10 minutes


if __name__ == "__main__":
    asyncio.run(main())
