import asyncio
import logging
import time
from datetime import datetime

import redis.asyncio as aioredis
from sqlalchemy import delete, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.models.backend import Backend
from app.models.backend_domain import BackendDomain
from app.models.domain import Domain
from app.models.job import Job
from app.services import config as config_service
from app.services.auth import decrypt_backend_key
from app.services.engine_client import EngineClient, EngineFullError
from app.services.redis_jobs import (
    dequeue_jobs,
    get_backend_inflight,
    get_job,
    publish_event,
    requeue_job,
    set_failed,
    set_running,
)
from app.services.storage import get_incoming_client, get_object

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("submit-worker")


async def main() -> None:
    settings = Settings()
    engine = create_async_engine(settings.database_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    r = aioredis.from_url(settings.redis_url, decode_responses=False)
    incoming_client = get_incoming_client(settings)
    engine_client = EngineClient()

    backends: list[Backend] = []
    backends_refreshed_at = 0.0
    health_cache: dict[int, tuple[bool, float]] = {}
    # Background domain-sync tasks, kept referenced so they aren't GC'd mid-flight
    # and their exceptions surface; gated per backend so concurrent rebuilds of the
    # same backend's domain set can't race.
    sync_tasks: set[asyncio.Task] = set()
    domains_synced_at: dict[int, float] = {}

    async def refresh_backends() -> None:
        nonlocal backends, backends_refreshed_at
        if time.time() - backends_refreshed_at < 30:
            return
        async with session_factory() as db:
            result = await db.execute(
                select(Backend).where(Backend.enabled == True).order_by(Backend.priority.desc())  # noqa: E712
            )
            backends = list(result.scalars().all())
            for b in backends:
                db.expunge(b)
        backends_refreshed_at = time.time()
        logger.info(f"Loaded {len(backends)} backend(s)")

    async def sync_domains(backend: Backend, api_key: str | None) -> None:
        """Fetch selectable_via_domain from the engine and rebuild backend_domains."""
        try:
            domain_names = await engine_client.get_models(backend.url, api_key)
        except Exception as exc:
            logger.debug("Domain sync failed for %s: %s", backend.url, exc)
            return
        async with session_factory() as db:
            # Upsert domain names
            domain_ids: list[int] = []
            for name in domain_names:
                await db.execute(
                    text("INSERT INTO domains (name) VALUES (:n) ON CONFLICT (name) DO NOTHING"),
                    {"n": name},
                )
                row = await db.execute(text("SELECT id FROM domains WHERE name = :n"), {"n": name})
                domain_ids.append(row.scalar_one())

            # Rebuild backend_domains for this backend (remove stale, add new)
            await db.execute(
                delete(BackendDomain).where(BackendDomain.backend_id == backend.id)
            )
            for did in domain_ids:
                await db.execute(
                    text(
                        "INSERT INTO backend_domains (backend_id, domain_id) VALUES (:b, :d) "
                        "ON CONFLICT DO NOTHING"
                    ),
                    {"b": backend.id, "d": did},
                )
            await db.commit()
            logger.debug("Synced %d domain(s) for backend %s", len(domain_ids), backend.url)

    def spawn_domain_sync(backend: Backend, api_key: str | None) -> None:
        """Kick off a background domain sync at most once per 5 min per backend."""
        if time.time() - domains_synced_at.get(backend.id, 0.0) < 300:
            return
        domains_synced_at[backend.id] = time.time()
        task = asyncio.create_task(sync_domains(backend, api_key))
        sync_tasks.add(task)
        task.add_done_callback(sync_tasks.discard)

    async def served_domains(backend_id: int) -> set[str]:
        """Domain names ``backend_id`` currently serves (from the synced mapping)."""
        async with session_factory() as db:
            rows = await db.execute(
                select(Domain.name)
                .join(BackendDomain, BackendDomain.domain_id == Domain.id)
                .where(BackendDomain.backend_id == backend_id)
            )
        return {name for (name,) in rows.all()}

    async def check_health(backend: Backend) -> bool:
        now = time.time()
        cached = health_cache.get(backend.id)
        if cached and now - cached[1] < 10:
            return cached[0]

        api_key = None
        if backend.api_key_enc:
            api_key = decrypt_backend_key(backend.api_key_enc, settings.key_encryption_secret)

        healthy = await engine_client.healthcheck(backend.url)
        health_cache[backend.id] = (healthy, now)
        if healthy:
            logger.info(f"Backend {backend.label or backend.url} healthy")
            # Refresh the backend's served domains in the background (best-effort).
            spawn_domain_sync(backend, api_key)
        else:
            logger.warning(f"Backend {backend.label or backend.url} unhealthy")
        return healthy

    async def dispatch(
        job_id: str, original_score: float, backend: Backend, backend_domains: set[str]
    ) -> None:
        meta = await get_job(r, job_id)
        if not meta:
            logger.warning(f"Job {job_id} not found in Redis, skipping")
            return

        username = meta["username"]
        external_id = meta["external_id"]
        ext = meta.get("ext", ".jpg")
        fmt = meta.get("fmt", "multi")
        domain = meta.get("domain", "") or None

        # Skip if this backend does not serve the requested domain. Checked against the
        # set loaded once per tick (no per-job DB round-trip); the job goes back on the
        # queue for a backend that does serve it.
        if domain and domain not in backend_domains:
            await requeue_job(r, job_id, original_score)
            return

        # Read image from MinIO
        object_path = f"{username}/{external_id}{ext}"
        try:
            image_bytes = await get_object(
                incoming_client, settings.minio_incoming_bucket, object_path
            )
        except Exception as e:
            logger.error(f"Failed to read image for job {job_id}: {e}")
            async with session_factory() as db:
                state_ttl = await config_service.get_state_ttl_seconds(db)
                await set_failed(r, job_id, f"Failed to read image: {e}", state_ttl)
                await db.execute(
                    update(Job).where(Job.id == job_id).values(status="failed", error=str(e))
                )
                await db.commit()
            await publish_event(
                r,
                username,
                {"status": "failed", "uuid": external_id, "error": str(e)},
            )
            return

        # Decrypt backend API key
        api_key = None
        if backend.api_key_enc:
            api_key = decrypt_backend_key(backend.api_key_enc, settings.key_encryption_secret)

        filename = f"{external_id}{ext}"
        try:
            engine_job_id = await engine_client.process(
                backend.url, api_key, image_bytes, filename, fmt, domain
            )
            logger.info(f"Job {job_id} running, engine_job_id={engine_job_id}")
            engine_version = await engine_client.get_version(backend.url)

            async with session_factory() as db:
                state_ttl = await config_service.get_state_ttl_seconds(db)
                await set_running(
                    r, job_id, engine_job_id, backend.url, backend.id, state_ttl
                )
                await db.execute(
                    update(Job)
                    .where(Job.id == job_id)
                    .values(
                        status="running",
                        dispatched_at=datetime.utcnow(),
                        engine_job_id=engine_job_id,
                        engine_version=engine_version,
                        backend_id=backend.id,
                    )
                )
                await db.commit()

        except EngineFullError:
            logger.info(f"Engine {backend.url} full, requeuing job {job_id}")
            await requeue_job(r, job_id, original_score)

        except Exception as e:
            logger.error(f"Failed to dispatch job {job_id}: {e}")
            async with session_factory() as db:
                state_ttl = await config_service.get_state_ttl_seconds(db)
                await set_failed(r, job_id, str(e), state_ttl)
                await db.execute(
                    update(Job)
                    .where(Job.id == job_id)
                    .values(
                        status="failed",
                        error=str(e),
                        finished_at=datetime.utcnow(),
                    )
                )
                await db.commit()
            await publish_event(
                r,
                username,
                {"status": "failed", "uuid": external_id, "error": str(e)},
            )

    logger.info("Submit worker started")
    while True:
        try:
            await refresh_backends()

            for backend in backends:
                if not await check_health(backend):
                    continue

                inflight = await get_backend_inflight(r, backend.id)
                slots = backend.max_inflight - inflight
                if slots <= 0:
                    continue

                job_entries = await dequeue_jobs(r, slots)
                if not job_entries:
                    continue

                logger.info(
                    f"Dispatching {len(job_entries)} job(s) to {backend.label or backend.url}"
                )
                backend_domains = await served_domains(backend.id)
                await asyncio.gather(
                    *[dispatch(jid, score, backend, backend_domains) for jid, score in job_entries]
                )

        except Exception as e:
            logger.error(f"Submit worker error: {e}")

        await asyncio.sleep(settings.submit_tick_seconds)


if __name__ == "__main__":
    asyncio.run(main())
