"""Submit worker: dequeues pending jobs and dispatches them to backend engines."""

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx
import redis.asyncio as aioredis
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clock import utcnow
from app.config import Settings
from app.engines import make_adapter
from app.engines.base import EngineAdapter, EngineFullError
from app.models.backend import Backend
from app.models.db import make_engine
from app.models.job import Job
from app.services import config as config_service
from app.services.auth import decrypt_backend_key
from app.services.domain_sync import all_served_capabilities, apply_domain_sync
from app.services.job_attempts import append_attempt
from app.services.redis_jobs import (
    dequeue_jobs,
    get_backend_inflight,
    get_jobs,
    job_domain,
    job_options,
    job_outputs,
    parse_avoid,
    promote_due_retries,
    publish_event,
    requeue_job,
    schedule_retry,
    set_failed,
    set_running,
    wait_for_submit_signal,
)
from app.services.retry_policy import (
    attempt_entry,
    backoff_seconds,
    describe_exception,
    http_status_of,
    is_retryable_dispatch_error,
)
from app.services.storage import get_incoming_client, get_object

logger = logging.getLogger("submit-worker")


@dataclass(frozen=True)
class PendingJob:
    """A job waiting to be dispatched, carrying its avoid-list from prior failures."""

    job_id: str
    score: float
    domain: str
    outputs: frozenset[str]
    avoid: frozenset[int] = field(default_factory=frozenset)


@dataclass
class BackendSlot:
    """A healthy backend, its remaining free capacity (may be 0) and what it serves:
    domain → the outputs it produces for that domain."""

    backend: Backend
    free: int
    domains: dict[str, frozenset[str]]


def serves(domains: dict[str, frozenset[str]], domain: str, outputs: frozenset[str]) -> bool:
    """Whether a backend serving ``domains`` can run a ``domain`` job wanting ``outputs``."""
    return domain in domains and outputs <= domains[domain]


def can_serve(slot: BackendSlot, job: PendingJob) -> bool:
    return serves(slot.domains, job.domain, job.outputs)


def allocate_jobs(
    slots: list[BackendSlot], jobs: list[PendingJob]
) -> tuple[list[tuple[PendingJob, BackendSlot]], list[PendingJob]]:
    """Deal one priority tier's jobs round-robin across its backends.

    Spreads jobs evenly rather than filling the first backend to capacity, so results
    come back with more parallelism across comparable backends. Respects each slot's
    free capacity and capabilities: a job only lands on a backend that serves its domain
    with all the outputs it wants (``can_serve``). A retried job skips the backends in
    its ``avoid`` set; it falls back to an avoided one only when no other backend in the
    tier can serve it at all (D7). If the alternatives are merely full, the job waits for
    them instead of going back to the backend that just failed it. Pure apart from
    decrementing the passed slots' ``free`` counters.

    Returns ``(assignments, leftovers)``; leftovers are jobs no slot in this tier can
    take right now (full, or their domain is served only by a lower tier) and should
    be re-queued.
    """
    assignments: list[tuple[PendingJob, BackendSlot]] = []
    leftovers: list[PendingJob] = []
    count = len(slots)
    start = 0
    # Which slots can serve a (domain, outputs) pair is the same for every job asking for
    # it, so it's worked out once per pair, not once per job and slot.
    serves_by_key: dict[tuple[str, frozenset[str]], list[bool]] = {}
    for job in jobs:
        key = (job.domain, job.outputs)
        mask = serves_by_key.get(key)
        if mask is None:
            mask = serves_by_key[key] = [can_serve(s, job) for s in slots]
        # Only a retried job has backends to avoid; the rest skip this check entirely.
        allow_avoided = bool(job.avoid) and all(
            s.backend.id in job.avoid for s, ok in zip(slots, mask, strict=True) if ok
        )
        placed = False
        for step in range(count):
            index = (start + step) % count
            slot = slots[index]
            if slot.free <= 0 or not mask[index]:
                continue
            if slot.backend.id in job.avoid and not allow_avoided:
                continue
            slot.free -= 1
            assignments.append((job, slot))
            start = (start + step + 1) % count  # next job starts after this backend
            placed = True
            break
        if not placed:
            leftovers.append(job)
    return assignments, leftovers


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # httpx emits one INFO line per request; at dispatch volume it floods the log and
    # rotates real errors (e.g. a failed dispatch's traceback) out of `kubectl logs`.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    engine = make_engine(settings)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    r = aioredis.from_url(settings.redis_url, decode_responses=False)
    incoming_client = get_incoming_client(settings)
    http = httpx.AsyncClient(timeout=60.0)
    # backend id -> (what it was built from, adapter); rebuilt when any of that changes.
    adapters: dict[int, tuple[tuple[str, str, str | None], EngineAdapter]] = {}

    backends: list[Backend] = []
    backends_refreshed_at = 0.0
    health_cache: dict[int, tuple[bool, float]] = {}
    # Background domain-sync tasks, kept referenced so they aren't GC'd mid-flight
    # and their exceptions surface; gated per backend so concurrent rebuilds of the
    # same backend's domain set can't race.
    sync_tasks: set[asyncio.Task[None]] = set()
    domains_synced_at: dict[int, float] = {}
    domain_sync_inflight: set[int] = set()
    # Every backend's capabilities, loaded in one query; 0.0 = reload on next use.
    capabilities: dict[int, dict[str, frozenset[str]]] = {}
    capabilities_loaded_at = 0.0

    def adapter_for(backend: Backend) -> EngineAdapter:
        """The backend's adapter, built once and rebuilt when its adapter id, URL or key
        changes (refresh_backends reloads the rows, e.g. after a Helm re-register)."""
        source = (backend.adapter, backend.url, backend.api_key_enc)
        cached = adapters.get(backend.id)
        if cached is not None and cached[0] == source:
            return cached[1]
        api_key = None
        if backend.api_key_enc:
            api_key = decrypt_backend_key(backend.api_key_enc, settings.key_encryption_secret)
        adapter = make_adapter(backend.adapter, backend.url, api_key, http)
        adapters[backend.id] = (source, adapter)
        return adapter

    async def refresh_backends() -> None:
        nonlocal backends, backends_refreshed_at
        if time.time() - backends_refreshed_at < settings.backend_refresh_seconds:
            return
        async with session_factory() as db:
            result = await db.execute(
                select(Backend).where(Backend.enabled.is_(True)).order_by(Backend.priority.desc())
            )
            backends = list(result.scalars().all())
            for b in backends:
                db.expunge(b)
        backends_refreshed_at = time.time()
        logger.info(f"Loaded {len(backends)} backend(s)")

    async def sync_domains(backend: Backend) -> None:
        """Fetch the engine's domains, outputs and options schema; rebuild backend_domains."""
        try:
            info = await adapter_for(backend).describe()
        except Exception as exc:
            logger.warning("Domain sync failed for %s: %s", backend.url, exc)
            return
        if not info.domains:
            # describe() reports nothing on a failed /models call; keep the last good
            # mapping rather than unlink the backend, and retry on the next tick.
            logger.warning("Domain sync for %s: engine advertised no domains", backend.url)
            return
        nonlocal capabilities_loaded_at
        async with session_factory() as db:
            await apply_domain_sync(db, backend.id, info)
            await db.commit()
        capabilities_loaded_at = 0.0  # this sync changed them: reload on the next tick
        domains_synced_at[backend.id] = time.time()
        logger.debug("Synced %d domain(s) for backend %s", len(info.domains), backend.url)

    def spawn_domain_sync(backend: Backend) -> None:
        """Kick off a background domain sync at most once per interval per backend.

        Stamps ``domains_synced_at`` only on success (in ``sync_domains``), so a failed
        sync retries on the next eligible tick; ``domain_sync_inflight`` prevents a second
        concurrent sync of the same backend from starting meanwhile.
        """
        if backend.id in domain_sync_inflight:
            return
        if time.time() - domains_synced_at.get(backend.id, 0.0) < settings.domain_sync_seconds:
            return
        domain_sync_inflight.add(backend.id)
        task = asyncio.create_task(sync_domains(backend))
        sync_tasks.add(task)

        def _done(t: asyncio.Task[None]) -> None:
            sync_tasks.discard(t)
            domain_sync_inflight.discard(backend.id)

        task.add_done_callback(_done)

    async def served_domains(backend_id: int) -> dict[str, frozenset[str]]:
        """Domain → outputs ``backend_id`` currently serves (from the synced mapping).

        Read from one query for all backends, reloaded right after this worker's own
        domain syncs and otherwise every ``backend_refresh_seconds``, which bounds how
        stale a change made elsewhere (another replica's sync, a declared registration)
        can be. The dispatch re-check reads the same snapshot.
        """
        nonlocal capabilities, capabilities_loaded_at
        if time.time() - capabilities_loaded_at >= settings.backend_refresh_seconds:
            async with session_factory() as db:
                capabilities = await all_served_capabilities(db)
            capabilities_loaded_at = time.time()
        return capabilities.get(backend_id, {})

    async def check_health(backend: Backend) -> bool:
        now = time.time()
        cached = health_cache.get(backend.id)
        if cached and now - cached[1] < settings.health_cache_seconds:
            return cached[0]

        healthy = await adapter_for(backend).healthy()
        health_cache[backend.id] = (healthy, now)
        if healthy:
            logger.info(f"Backend {backend.label or backend.url} healthy")
            # Refresh the backend's served domains in the background (best-effort).
            spawn_domain_sync(backend)
        else:
            logger.warning(f"Backend {backend.label or backend.url} unhealthy")
        return healthy

    async def dispatch(
        job_id: str,
        original_score: float,
        backend: Backend,
        backend_domains: dict[str, frozenset[str]],
        meta: dict[str, str] | None,
    ) -> None:
        # meta was read this tick, with every other dequeued job's, in one round trip.
        if not meta:
            logger.warning(f"Job {job_id} not found in Redis, skipping")
            return

        username = meta["username"]
        external_id = meta["external_id"]
        ext = meta.get("ext", ".jpg")
        domain = job_domain(meta)
        outputs = job_outputs(meta)

        # Skip if this backend does not serve the job's domain + outputs. Checked against
        # the capabilities loaded once per tick (no per-job DB round-trip); the job goes
        # back on the queue for a backend that does serve it.
        if not serves(backend_domains, domain, outputs):
            await requeue_job(r, job_id, original_score)
            return

        # Read image from MinIO — keyed by job_id (the server PK), matching the submit upload.
        object_path = f"{username}/{job_id}{ext}"
        try:
            image_bytes = await get_object(
                incoming_client, settings.minio_incoming_bucket, object_path
            )
        except Exception as e:
            logger.exception("Failed to read image for job %s", job_id)
            msg = f"Failed to read image: {describe_exception(e)}"
            attempt = attempt_entry(
                "dispatch", backend.id, None, msg, retry=False, at=utcnow().isoformat()
            )
            async with session_factory() as db:
                state_ttl = await config_service.get_state_ttl_seconds(db)
                await set_failed(r, job_id, msg, state_ttl)
                await db.execute(
                    update(Job)
                    .where(Job.id == job_id)
                    .values(
                        status="failed", error=msg, finished_at=utcnow(), **append_attempt(attempt)
                    )
                )
                await db.commit()
            await publish_event(
                r,
                username,
                {"status": "failed", "uuid": external_id, "error": msg},
            )
            return

        filename = f"{external_id}{ext}"
        try:
            adapter = adapter_for(backend)
            engine_job_id = await adapter.submit(
                image_bytes, filename, domain, outputs, job_options(meta)
            )
            logger.info(f"Job {job_id} running, engine_job_id={engine_job_id}")
            engine_version = await adapter.version()

            async with session_factory() as db:
                state_ttl = await config_service.get_state_ttl_seconds(db)
                await set_running(r, job_id, engine_job_id, backend.url, backend.id, state_ttl)
                await db.execute(
                    update(Job)
                    .where(Job.id == job_id)
                    .values(
                        status="running",
                        dispatched_at=utcnow(),
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
            msg = describe_exception(e)
            transient = is_retryable_dispatch_error(e)
            http_st = http_status_of(e)
            async with session_factory() as db:
                max_requeues = await config_service.get_max_requeues(db)
                state_ttl = await config_service.get_state_ttl_seconds(db)
            requeues = int(meta.get("requeues", 0))

            # Transient failure (backend down/overloaded) with budget left: sideline this
            # backend for the health-cache window so the retry lands elsewhere, bump the
            # requeue counter the budget reads (Redis + DB), and schedule a delayed retry.
            if transient and requeues < max_requeues:
                delay = backoff_seconds(requeues)
                logger.warning(
                    "Transient dispatch error for job %s on %s (attempt %d/%d), "
                    "requeue in %.1fs: %s",
                    job_id,
                    backend.url,
                    requeues + 1,
                    max_requeues,
                    delay,
                    msg,
                )
                health_cache[backend.id] = (False, time.time())
                await r.hincrby(f"job:{job_id}", "requeues", 1)
                await schedule_retry(r, job_id, original_score, delay, avoid_backend_id=backend.id)
                attempt = attempt_entry(
                    "dispatch", backend.id, http_st, msg, retry=True, at=utcnow().isoformat()
                )
                async with session_factory() as db:
                    await db.execute(
                        update(Job)
                        .where(Job.id == job_id)
                        .values(
                            requeues=Job.requeues + 1,
                            **append_attempt(attempt),
                        )
                    )
                    await db.commit()
                return

            # Terminal: not transient, or the retry budget is spent.
            if transient:
                logger.error(
                    "Dispatch for job %s failed after %d requeue attempts: %s",
                    job_id,
                    max_requeues,
                    msg,
                )
                msg = f"{msg} (exceeded {max_requeues} requeue attempts)"
            else:
                logger.exception("Failed to dispatch job %s", job_id)
            attempt = attempt_entry(
                "dispatch", backend.id, http_st, msg, retry=False, at=utcnow().isoformat()
            )
            async with session_factory() as db:
                await set_failed(r, job_id, msg, state_ttl)
                await db.execute(
                    update(Job)
                    .where(Job.id == job_id)
                    .values(
                        status="failed",
                        error=msg,
                        finished_at=utcnow(),
                        **append_attempt(attempt),
                    )
                )
                await db.commit()
            await publish_event(
                r,
                username,
                {"status": "failed", "uuid": external_id, "error": msg},
            )

    logger.info("Submit worker started")
    try:
        while True:
            try:
                await refresh_backends()

                # Promote delayed retries that are now due.
                promoted = await promote_due_retries(r, time.time())
                if promoted:
                    logger.info(f"Promoted {promoted} delayed retry/requeue(s)")

                # This tick's eligible backends: healthy, with their free capacity + served
                # domains (each looked up once). Full ones stay in (free=0) so a retry
                # waits for them rather than falling back to the backend it avoids.
                eligible: list[BackendSlot] = []
                for backend in backends:
                    if not await check_health(backend):
                        continue
                    free = backend.max_inflight - await get_backend_inflight(r, backend.id)
                    domains = await served_domains(backend.id)
                    eligible.append(BackendSlot(backend, max(free, 0), domains))

                # Strict priority tiers: a higher-priority tier drains first; within a tier
                # spread jobs round-robin so no single backend fills before its peers.
                for priority in sorted({slot.backend.priority for slot in eligible}, reverse=True):
                    tier = [slot for slot in eligible if slot.backend.priority == priority]
                    capacity = sum(slot.free for slot in tier)
                    if capacity == 0:
                        continue  # this tier is full; a lower one may still have room
                    entries = await dequeue_jobs(r, capacity)
                    if not entries:
                        break  # nothing pending — lower tiers have nothing to do either

                    metas = await get_jobs(r, [job_id for job_id, _ in entries])
                    pending: list[PendingJob] = []
                    for job_id, score in entries:
                        meta = metas[job_id] or {}
                        domain, outputs = job_domain(meta), job_outputs(meta)
                        pending.append(
                            PendingJob(job_id, score, domain, outputs, parse_avoid(meta))
                        )

                    assignments, leftovers = allocate_jobs(tier, pending)
                    for job in leftovers:
                        # a lower tier may serve its domain, or a full backend frees up
                        await requeue_job(r, job.job_id, job.score)

                    if not assignments:
                        continue

                    logger.info(
                        f"Dispatching {len(assignments)} job(s) across {len(tier)} backend(s) "
                        f"at priority {priority}"
                    )
                    results = await asyncio.gather(
                        *[
                            dispatch(
                                job.job_id, job.score, slot.backend, slot.domains, metas[job.job_id]
                            )
                            for job, slot in assignments
                        ],
                        return_exceptions=True,
                    )
                    for result in results:
                        if isinstance(result, Exception):
                            logger.error("Dispatch failed: %s", result)

            except Exception:
                logger.exception("Submit worker failed")

            # Block until new work / a freed slot signals (signal_submit), with the tick
            # as an idle-wait floor. Event-driven so fast jobs refill slots immediately
            # and slow jobs don't busy-poll.
            await wait_for_submit_signal(r, settings.submit_tick_seconds)
    finally:
        await http.aclose()
        await r.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
