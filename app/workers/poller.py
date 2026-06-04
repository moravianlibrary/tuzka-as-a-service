import asyncio
import logging
import time
from datetime import datetime

import redis.asyncio as aioredis
import zstandard
from sqlalchemy import update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.models.backend import Backend
from app.models.job import Job, JobResult
from app.services.auth import decrypt_backend_key
from app.services.engine_client import EngineClient
from app.services.redis_jobs import (
    get_inflight_ids,
    get_job,
    publish_event,
    set_done,
    set_failed,
)
from app.services.storage import get_results_client, presign_get, put_object

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("poller-worker")


async def main() -> None:
    settings = Settings()
    db_engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    r = aioredis.from_url(settings.redis_url, decode_responses=False)
    engine_client = EngineClient()
    results_client = get_results_client(settings)
    cctx = zstandard.ZstdCompressor(level=settings.zstd_compression_level)

    backend_keys: dict[int, str | None] = {}

    async def get_backend_api_key(backend_id: int) -> str | None:
        if backend_id not in backend_keys:
            async with session_factory() as db:
                backend = await db.get(Backend, backend_id)
            key = None
            if backend and backend.api_key_enc:
                key = decrypt_backend_key(backend.api_key_enc, settings.key_encryption_secret)
            backend_keys[backend_id] = key
        return backend_keys[backend_id]

    async def check_one(job_id: str) -> tuple[str, str, dict]:
        meta = await get_job(r, job_id)
        if not meta:
            return (job_id, "unknown", {})

        api_key = None
        if meta.get("backend_id"):
            api_key = await get_backend_api_key(int(meta["backend_id"]))

        try:
            resp = await engine_client.check_status(
                meta["backend_url"], api_key, meta["engine_job_id"]
            )
            return (job_id, resp.get("status", "unknown"), meta)
        except Exception as e:
            logger.error(f"Failed to check status for job {job_id}: {e}")
            return (job_id, "error", meta)

    async def harvest(job_id: str, meta: dict) -> None:
        username = meta["username"]
        external_id = meta["external_id"]
        fmt = meta.get("fmt", "multi")
        backend_url = meta["backend_url"]
        engine_job_id = meta["engine_job_id"]

        api_key = None
        if meta.get("backend_id"):
            api_key = await get_backend_api_key(int(meta["backend_id"]))

        try:
            results_to_store = []
            if fmt == "multi":
                alto_bytes = await engine_client.get_result(
                    backend_url, api_key, engine_job_id, which="alto"
                )
                txt_bytes = await engine_client.get_result(
                    backend_url, api_key, engine_job_id, which="txt"
                )
                results_to_store = [
                    ("alto", f"{username}/{external_id}.xml.zst", alto_bytes),
                    ("txt", f"{username}/{external_id}.txt.zst", txt_bytes),
                ]
            elif fmt == "alto":
                alto_bytes = await engine_client.get_result(backend_url, api_key, engine_job_id)
                results_to_store = [
                    ("alto", f"{username}/{external_id}.xml.zst", alto_bytes),
                ]
            else:
                txt_bytes = await engine_client.get_result(backend_url, api_key, engine_job_id)
                results_to_store = [
                    ("txt", f"{username}/{external_id}.txt.zst", txt_bytes),
                ]

            event_data: dict = {
                "status": "done",
                "uuid": external_id,
            }

            async with session_factory() as db:
                for result_fmt, obj_path, raw_bytes in results_to_store:
                    compressed = cctx.compress(raw_bytes)
                    await put_object(
                        results_client,
                        settings.minio_results_bucket,
                        obj_path,
                        compressed,
                        "application/octet-stream",
                    )
                    logger.info(f"Stored {obj_path} ({len(compressed)} bytes)")

                    presigned_url = await presign_get(
                        results_client,
                        settings.minio_results_bucket,
                        obj_path,
                        settings.presigned_ttl_minutes,
                    )

                    from datetime import timedelta

                    jr = JobResult(
                        job_id=job_id,
                        fmt=result_fmt,
                        presigned_url=presigned_url,
                        presigned_until=datetime.utcnow()
                        + timedelta(minutes=settings.presigned_ttl_minutes),
                    )
                    db.add(jr)

                    url_key = "alto_url" if result_fmt == "alto" else "txt_url"
                    event_data[url_key] = presigned_url

                await db.execute(
                    update(Job)
                    .where(Job.id == job_id)
                    .values(status="done", finished_at=datetime.utcnow())
                )
                await db.commit()

            await set_done(r, job_id)
            await publish_event(r, username, event_data)
            logger.info(f"Published done event for {username}")

        except Exception as e:
            logger.error(f"Failed to harvest job {job_id}: {e}")
            await mark_failed(job_id, meta, str(e))

    async def mark_failed(job_id: str, meta: dict, error: str) -> None:
        username = meta.get("username", "")
        external_id = meta.get("external_id", "")
        await set_failed(r, job_id, error)
        async with session_factory() as db:
            await db.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(
                    status="failed",
                    error=error,
                    finished_at=datetime.utcnow(),
                )
            )
            await db.commit()
        await publish_event(
            r,
            username,
            {"status": "failed", "uuid": external_id, "error": error},
        )

    sem = asyncio.Semaphore(settings.poller_harvest_concurrency)

    async def harvest_with_sem(job_id: str, meta: dict) -> None:
        async with sem:
            await harvest(job_id, meta)

    logger.info("Poller worker started")
    while True:
        try:
            job_ids = await get_inflight_ids(r)
            if not job_ids:
                await asyncio.sleep(settings.poller_tick_seconds)
                continue

            now = time.time()
            due = []
            for jid in job_ids:
                meta = await get_job(r, jid)
                if not meta:
                    continue
                next_poll = float(meta.get("next_poll_at", 0))
                if next_poll <= now:
                    due.append(jid)

            if not due:
                await asyncio.sleep(settings.poller_tick_seconds)
                continue

            logger.info(f"Checking {len(due)} inflight jobs")
            statuses = await asyncio.gather(*[check_one(jid) for jid in due])

            done_jobs = []
            failed_jobs = []
            running_jobs = []

            for job_id, status, meta in statuses:
                if status == "done":
                    done_jobs.append((job_id, meta))
                elif status in ("failed", "error"):
                    failed_jobs.append((job_id, meta, meta.get("error", "Engine error")))
                else:
                    running_jobs.append((job_id, meta))

            # Update next_poll_at with backoff for running jobs
            for job_id, meta in running_jobs:
                current_backoff = float(meta.get("next_poll_at", time.time())) - float(
                    meta.get("last_poll", time.time())
                )
                next_backoff = min(
                    max(current_backoff * 2, settings.poll_backoff_initial),
                    settings.poll_backoff_max,
                )
                await r.hset(
                    f"job:{job_id}",
                    mapping={
                        "next_poll_at": str(time.time() + next_backoff),
                        "last_poll": str(time.time()),
                    },
                )

            # Harvest done jobs
            if done_jobs:
                await asyncio.gather(*[harvest_with_sem(jid, meta) for jid, meta in done_jobs])

            # Mark failed jobs
            for job_id, meta, error in failed_jobs:
                await mark_failed(job_id, meta, error)

        except Exception as e:
            logger.error(f"Poller worker error: {e}")

        await asyncio.sleep(settings.poller_tick_seconds)


if __name__ == "__main__":
    asyncio.run(main())
