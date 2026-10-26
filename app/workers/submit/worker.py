"""Submit worker: dequeues pending jobs and dispatches them to backend engines."""

import asyncio
import logging
import time
from collections.abc import Iterable
from typing import Any

import httpx
import redis.asyncio as aioredis
from miniopy_async.api import Minio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import utcnow
from app.config import Settings
from app.engines.base import EngineFullError
from app.engines.cache import AdapterCache
from app.models.backend import Backend
from app.models.db import make_engine, make_session_factory
from app.models.job import Job
from app.schemas.attempt import Attempt
from app.schemas.redis import JobState
from app.services import batch_store, live_metrics, perf
from app.services import config as config_service
from app.services.domain_sync import all_served_capabilities, apply_domain_sync
from app.services.event_emitter import EventEmitter, get_emitter
from app.services.events import JobEvent, failure
from app.services.job_attempts import append_attempt
from app.services.redis_jobs import (
    batch_event,
    claim_pending,
    count_requeue,
    drop_pending,
    get_backends_state,
    get_jobs,
    job_event_payload,
    peek_pending,
    promote_due_retries,
    publish_event,
    requeue_job,
    schedule_retry,
    set_failed,
    set_running,
    unstored_depth,
    wait_for_submit_signal,
)
from app.services.redis_pool import make_redis
from app.services.retry_policy import (
    backoff_seconds,
    describe_exception,
    http_status_of,
    is_retryable_dispatch_error,
)
from app.services.storage import get_incoming_client, get_object
from app.telemetry import job_span, setup_telemetry
from app.vocabulary import FailureStage, JobEventName, JobStatus
from app.workers.shutdown import install_stop
from app.workers.stages import StageTimer, log_stages, stage_metric_rows
from app.workers.submit.steps import allocate_scored, pending_job, serves, unstored_capacity
from app.workers.submit.types import BackendSlot

logger = logging.getLogger("submit-worker")


# Seconds per megapixel for a domain nothing has been measured on: step 4's measured
# seconds per page (load:spp) taken as one megapixel a page, else this.
DEFAULT_SECONDS_PER_MP = 10.0


async def _fallback_spp(r: aioredis.Redis, domains: Iterable[str]) -> dict[str, float]:
    names = sorted(set(domains))
    values = await r.hmget("load:spp", names) if names else []
    return {
        name: float(value) if value is not None else DEFAULT_SECONDS_PER_MP
        for name, value in zip(names, values, strict=True)
    }


class SubmitWorker:
    """The submit worker's long-lived clients and caches, and the steps of a tick."""

    def __init__(
        self,
        settings: Settings,
        r: aioredis.Redis,
        session_factory: async_sessionmaker[AsyncSession],
        http: httpx.AsyncClient,
        incoming_client: Minio,
        emitter: EventEmitter,
        stop: asyncio.Event,
    ) -> None:
        self.settings = settings
        self.r = r
        self.session_factory = session_factory
        self.http = http
        self.incoming_client = incoming_client
        self.emitter = emitter
        self.stop = stop
        self.timer = StageTimer()
        self.stages_flushed_at = time.monotonic()
        self.adapters = AdapterCache(http, settings.key_encryption_secret)
        self.backends: list[Backend] = []
        self.backends_refreshed_at = 0.0
        self.health_cache: dict[int, tuple[bool, float]] = {}
        # Background domain-sync tasks, kept referenced so they aren't GC'd mid-flight
        # and their exceptions surface; gated per backend so concurrent rebuilds of the
        # same backend's domain set can't race.
        self.sync_tasks: set[asyncio.Task[None]] = set()
        self.domains_synced_at: dict[int, float] = {}
        self.domain_sync_inflight: set[int] = set()
        # Every backend's capabilities, loaded in one query; 0.0 = reload on next use.
        self.capabilities: dict[int, dict[str, frozenset[str]]] = {}
        self.capabilities_loaded_at = 0.0
        self.paused_for_harvest = False

    async def refresh_backends(self) -> None:
        if time.time() - self.backends_refreshed_at < self.settings.backend_refresh_seconds:
            return
        async with self.session_factory() as db:
            result = await db.execute(
                select(Backend).where(Backend.enabled.is_(True)).order_by(Backend.priority.desc())
            )
            self.backends = list(result.scalars().all())
            for b in self.backends:
                db.expunge(b)
        self.backends_refreshed_at = time.time()
        logger.info(f"Loaded {len(self.backends)} backend(s)")

    async def sync_domains(self, backend: Backend) -> None:
        """Fetch the engine's domains, outputs and options schema; rebuild backend_domains."""
        try:
            info = await self.adapters.for_backend(backend, backend.url).describe()
        except Exception as exc:
            logger.warning("Domain sync failed for %s: %s", backend.url, exc)
            return
        if not info.domains:
            # describe() reports nothing on a failed /models call; keep the last good
            # mapping rather than unlink the backend, and retry on the next tick.
            logger.warning("Domain sync for %s: engine advertised no domains", backend.url)
            return
        async with self.session_factory() as db:
            await apply_domain_sync(db, backend.id, info)
            await db.commit()
        self.capabilities_loaded_at = 0.0  # this sync changed them: reload on the next tick
        self.domains_synced_at[backend.id] = time.time()
        logger.debug("Synced %d domain(s) for backend %s", len(info.domains), backend.url)

    def spawn_domain_sync(self, backend: Backend) -> None:
        """Kick off a background domain sync at most once per interval per backend.

        Stamps ``domains_synced_at`` only on success (in ``sync_domains``), so a failed
        sync retries on the next eligible tick; ``domain_sync_inflight`` prevents a second
        concurrent sync of the same backend from starting meanwhile.
        """
        if backend.id in self.domain_sync_inflight:
            return
        synced_at = self.domains_synced_at.get(backend.id, 0.0)
        if time.time() - synced_at < self.settings.domain_sync_seconds:
            return
        self.domain_sync_inflight.add(backend.id)
        task = asyncio.create_task(self.sync_domains(backend))
        self.sync_tasks.add(task)

        def _done(t: asyncio.Task[None]) -> None:
            self.sync_tasks.discard(t)
            self.domain_sync_inflight.discard(backend.id)

        task.add_done_callback(_done)

    async def served_domains(self, backend_id: int) -> dict[str, frozenset[str]]:
        """Domain → outputs ``backend_id`` currently serves (from the synced mapping).

        Read from one query for all backends, reloaded right after this worker's own
        domain syncs and otherwise every ``backend_refresh_seconds``, which bounds how
        stale a change made elsewhere (another replica's sync, a declared registration)
        can be. The dispatch re-check reads the same snapshot.
        """
        if time.time() - self.capabilities_loaded_at >= self.settings.backend_refresh_seconds:
            async with self.session_factory() as db:
                self.capabilities = await all_served_capabilities(db)
            self.capabilities_loaded_at = time.time()
        return self.capabilities.get(backend_id, {})

    async def check_health(self, backend: Backend) -> bool:
        now = time.time()
        cached = self.health_cache.get(backend.id)
        if cached and now - cached[1] < self.settings.health_cache_seconds:
            return cached[0]

        healthy = await self.adapters.for_backend(backend, backend.url).healthy()
        self.health_cache[backend.id] = (healthy, now)
        if healthy:
            logger.info(f"Backend {backend.label or backend.url} healthy")
            # Refresh the backend's served domains in the background (best-effort).
            self.spawn_domain_sync(backend)
        else:
            logger.warning(f"Backend {backend.label or backend.url} unhealthy")
        return healthy

    async def dispatch(
        self,
        job_id: str,
        original_score: float,
        backend: Backend,
        backend_domains: dict[str, frozenset[str]],
        state: JobState,
    ) -> None:
        # A child of the job's submit trace, so the engine calls below land in it.
        with job_span("dispatch", job_id, state, backend_id=backend.id):
            await self.dispatch_job(job_id, original_score, backend, backend_domains, state)

    async def dispatch_job(  # noqa: C901, PLR0915 - split up on refactor/complexity
        self,
        job_id: str,
        original_score: float,
        backend: Backend,
        backend_domains: dict[str, frozenset[str]],
        state: JobState,
    ) -> None:
        r = self.r
        username = state.username
        external_id = state.external_id
        ext = state.ext
        domain = state.domain
        outputs = state.outputs

        def event(name: JobEventName, **fields: Any) -> JobEvent:
            return JobEvent.from_state(
                utcnow(),
                name,
                job_id,
                state,
                backend_id=backend.id,
                backend=backend.display_label,
                adapter=backend.adapter,
                device=backend.device,
                **fields,
            )

        # Skip if this backend does not serve the job's domain + outputs. Checked against
        # the capabilities loaded once per tick (no per-job DB round-trip); the job goes
        # back on the queue for a backend that does serve it.
        if not serves(backend_domains, domain, outputs):
            await requeue_job(r, job_id, original_score)
            return

        # Read image from MinIO — keyed by job_id (the server PK), matching the submit upload.
        object_path = f"{username}/{job_id}{ext}"
        try:
            with self.timer.stage("image_get"):
                image_bytes = await get_object(
                    self.incoming_client, self.settings.minio_incoming_bucket, object_path
                )
        except Exception as e:
            logger.exception("Failed to read image for job %s", job_id)
            msg = f"Failed to read image: {describe_exception(e)}"
            attempt = Attempt(
                at=utcnow(),
                stage=FailureStage.DISPATCH,
                backend_id=backend.id,
                http_status=None,
                error=msg,
                retry=False,
            )
            async with self.session_factory() as db:
                state_ttl = await config_service.get_state_ttl_seconds(db)
                batch = await set_failed(r, job_id, msg, state_ttl)
                await db.execute(
                    update(Job)
                    .where(Job.id == job_id)
                    .values(
                        status=JobStatus.FAILED,
                        error=msg,
                        finished_at=utcnow(),
                        **append_attempt(attempt),
                    )
                )
                await batch_store.finish_if_done(db, state.batch_id)
                await db.commit()
            self.emitter.emit(event(JobEventName.FAILED, **failure(FailureStage.DISPATCH, None)))
            await publish_event(
                r,
                username,
                job_event_payload(
                    job_id, external_id, JobStatus.FAILED, batch_id=state.batch_id, error=msg
                ),
            )
            if batch is not None:
                await publish_event(r, username, batch_event(batch))
            return

        # Named by job id: the external id is the caller's free text, not a file name.
        filename = f"{job_id}{ext}"
        try:
            adapter = self.adapters.for_backend(backend, backend.url)
            with self.timer.stage("engine_submit"):
                engine_job_id = await adapter.submit(
                    image_bytes, filename, domain, outputs, state.options
                )
            logger.info(f"Job {job_id} running, engine_job_id={engine_job_id}")
            with self.timer.stage("engine_version"):
                engine_version = await adapter.version()

            async with self.session_factory() as db:
                state_ttl = await config_service.get_state_ttl_seconds(db)
                with self.timer.stage("set_running"):
                    await set_running(r, job_id, engine_job_id, backend.url, backend.id, state_ttl)
                with self.timer.stage("db_update"):
                    await db.execute(
                        update(Job)
                        .where(Job.id == job_id)
                        .values(
                            status=JobStatus.RUNNING,
                            dispatched_at=utcnow(),
                            engine_job_id=engine_job_id,
                            engine_version=engine_version,
                            backend_id=backend.id,
                        )
                    )
                    await db.commit()
            submitted_at = state.submitted_at
            self.emitter.emit(
                event(
                    JobEventName.DISPATCHED,
                    engine_version=engine_version or "",
                    queue_s=max(time.time() - submitted_at, 0.0),
                )
            )

        except EngineFullError:
            logger.info(f"Engine {backend.url} full, requeuing job {job_id}")
            await requeue_job(r, job_id, original_score)

        except Exception as e:
            msg = describe_exception(e)
            transient = is_retryable_dispatch_error(e)
            http_st = http_status_of(e)
            failed_fields = failure(
                FailureStage.DISPATCH, http_st, isinstance(e, httpx.TransportError)
            )
            async with self.session_factory() as db:
                max_requeues = (await config_service.get_runtime_config(db)).max_requeues
                state_ttl = await config_service.get_state_ttl_seconds(db)
            requeues = state.requeues

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
                self.health_cache[backend.id] = (False, time.time())
                await count_requeue(r, job_id)
                await schedule_retry(r, job_id, original_score, delay, avoid_backend_id=backend.id)
                await perf.record_failure(r, backend.id, domain)
                attempt = Attempt(
                    at=utcnow(),
                    stage=FailureStage.DISPATCH,
                    backend_id=backend.id,
                    http_status=http_st,
                    error=msg,
                    retry=True,
                )
                async with self.session_factory() as db:
                    await db.execute(
                        update(Job)
                        .where(Job.id == job_id)
                        .values(
                            requeues=Job.requeues + 1,
                            **append_attempt(attempt),
                        )
                    )
                    await db.commit()
                self.emitter.emit(event(JobEventName.RETRIED, **failed_fields))
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
            attempt = Attempt(
                at=utcnow(),
                stage=FailureStage.DISPATCH,
                backend_id=backend.id,
                http_status=http_st,
                error=msg,
                retry=False,
            )
            if perf.counts_against_backend(http_st):
                await perf.record_failure(r, backend.id, domain)
            async with self.session_factory() as db:
                batch = await set_failed(r, job_id, msg, state_ttl)
                await db.execute(
                    update(Job)
                    .where(Job.id == job_id)
                    .values(
                        status=JobStatus.FAILED,
                        error=msg,
                        finished_at=utcnow(),
                        **append_attempt(attempt),
                    )
                )
                await batch_store.finish_if_done(db, state.batch_id)
                await db.commit()
            self.emitter.emit(event(JobEventName.FAILED, **failed_fields))
            await publish_event(
                r,
                username,
                job_event_payload(
                    job_id, external_id, JobStatus.FAILED, batch_id=state.batch_id, error=msg
                ),
            )
            if batch is not None:
                await publish_event(r, username, batch_event(batch))

    async def run(self) -> None:  # noqa: C901, PLR0912, PLR0915 - split up on refactor/complexity
        settings = self.settings
        r = self.r
        timer = self.timer
        logger.info("Submit worker started")
        while not self.stop.is_set():
            try:
                with timer.stage("refresh"):
                    await self.refresh_backends()

                # Promote delayed retries that are now due.
                with timer.stage("promote"):
                    promoted = await promote_due_retries(r, time.time())
                if promoted:
                    logger.info(f"Promoted {promoted} delayed retry/requeue(s)")

                # This tick's eligible backends: healthy, with their free capacity + served
                # domains (each looked up once). Full ones stay in (free=0) so a retry
                # waits for them rather than falling back to the backend it avoids.
                healthy_backends: list[Backend] = []
                for backend in self.backends:
                    with timer.stage("health"):
                        healthy = await self.check_health(backend)
                    if healthy:
                        healthy_backends.append(backend)
                with timer.stage("backend_state"):
                    backend_states = await get_backends_state(
                        r, [backend.id for backend in healthy_backends]
                    )
                eligible: list[BackendSlot] = []
                for backend in healthy_backends:
                    inflight, batches = backend_states[backend.id]
                    domains = await self.served_domains(backend.id)
                    eligible.append(
                        BackendSlot(
                            backend,
                            max(backend.max_inflight - inflight, 0),
                            domains,
                            inflight=inflight,
                            batches=batches,
                        )
                    )

                # In-flight engine work will also need storage, so count it against
                # the same budget as jobs already waiting for harvest.
                inflight_depth, harvest_depth = await unstored_depth(r)
                remaining = unstored_capacity(
                    settings.unstored_job_max, inflight_depth, harvest_depth
                )
                if remaining == 0:
                    if not self.paused_for_harvest:
                        logger.warning(
                            "Unstored jobs at limit (running=%d, storing=%d, limit=%d); "
                            "pausing dispatch",
                            inflight_depth,
                            harvest_depth,
                            settings.unstored_job_max,
                        )
                        self.paused_for_harvest = True
                    eligible = []
                elif self.paused_for_harvest:
                    logger.info("Unstored jobs below limit; resuming dispatch")
                    self.paused_for_harvest = False

                # Strict priority tiers: a higher-priority tier drains first; within a tier
                # spread jobs round-robin so no single backend fills before its peers.
                for priority in sorted({slot.backend.priority for slot in eligible}, reverse=True):
                    tier = [slot for slot in eligible if slot.backend.priority == priority]
                    capacity = min(sum(slot.free for slot in tier), remaining)
                    if capacity == 0:
                        continue  # this tier is full; a lower one may still have room
                    # Peeked, not popped: a job leaves its queue only once claimed below,
                    # so one no backend serves yet, or one in hand when this worker dies,
                    # stays queued.
                    with timer.stage("peek"):
                        entries = await peek_pending(r, capacity)
                    if not entries:
                        break  # nothing pending — lower tiers have nothing to do either

                    with timer.stage("meta"):
                        found = await get_jobs(r, [entry.job_id for entry in entries])
                    # A job whose state expired or was deleted can't be dispatched; left
                    # queued it would take a peek slot every tick.
                    gone = [entry for entry in entries if found[entry.job_id] is None]
                    if gone:
                        await drop_pending(r, gone)
                        logger.warning(
                            "Dropped %d queued job(s) whose state is gone: %s",
                            len(gone),
                            [entry.job_id for entry in gone],
                        )
                    states = {job_id: state for job_id, state in found.items() if state is not None}
                    if not states:
                        continue
                    priorities = {entry.job_id: entry.priority for entry in entries}
                    pending = [
                        pending_job(entry, states[entry.job_id])
                        for entry in entries
                        if entry.job_id in states
                    ]

                    # Speed scores for this tier's backends and these jobs' domains, once.
                    job_domains = {job.domain for job in pending}
                    with timer.stage("scores"):
                        scores = await perf.load_scores(
                            r, [slot.backend.id for slot in tier], job_domains
                        )
                        fallback = await _fallback_spp(r, job_domains)
                        async with self.session_factory() as db:
                            tolerance = (
                                await config_service.get_runtime_config(db)
                            ).affinity_tolerance

                    def spp(
                        backend_id: int,
                        domain: str,
                        scores: perf.Scores = scores,
                        fallback: dict[str, float] = fallback,
                    ) -> float:
                        return perf.effective_spp(backend_id, domain, scores, fallback[domain])

                    # Leftovers stay queued for a lower tier or a backend that frees up.
                    with timer.stage("allocate"):
                        assignments, _ = allocate_scored(tier, pending, spp, tolerance)
                    # Another submit worker (two run during a rollout) may have claimed a
                    # job first; it's dispatched there, not twice.
                    with timer.stage("claim"):
                        assignments = [
                            (job, slot)
                            for job, slot in assignments
                            if await claim_pending(r, job.job_id, priorities[job.job_id])
                        ]
                    if not assignments:
                        continue

                    remaining -= len(assignments)

                    logger.info(
                        f"Dispatching {len(assignments)} job(s) across {len(tier)} backend(s) "
                        f"at priority {priority}"
                    )
                    with timer.stage("dispatch"):
                        results = await asyncio.gather(
                            *[
                                self.dispatch(
                                    job.job_id,
                                    job.score,
                                    slot.backend,
                                    slot.domains,
                                    states[job.job_id],
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

            if time.monotonic() - self.stages_flushed_at >= 60:
                self.stages_flushed_at = time.monotonic()
                p95 = timer.p95()
                totals = timer.flush()
                log_stages(logger, totals, p95)
                if settings.clickhouse_url and totals:
                    try:
                        await live_metrics.insert_rows(
                            self.http, settings, stage_metric_rows("submit", totals)
                        )
                    except Exception as exc:
                        logger.warning("Stage metrics dropped: %s", exc)

            # Block until new work / a freed slot signals (signal_submit), with the tick
            # as an idle-wait floor. Event-driven so fast jobs refill slots immediately
            # and slow jobs don't busy-poll.
            try:
                await wait_for_submit_signal(r, settings.submit_tick_seconds)
            except Exception:  # a Redis hiccup must not end the worker
                logger.exception("Waiting for the submit signal failed")
                await asyncio.sleep(settings.submit_tick_seconds)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # httpx emits one INFO line per request; at dispatch volume it floods the log and
    # rotates real errors (e.g. a failed dispatch's traceback) out of `kubectl logs`.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    engine = make_engine(settings)
    setup_telemetry(settings, "taas-submit", db_engine=engine)
    r = make_redis(settings)
    http = httpx.AsyncClient(timeout=60.0)
    emitter = get_emitter(settings)
    await emitter.start()
    worker = SubmitWorker(
        settings,
        r,
        make_session_factory(engine),
        http,
        get_incoming_client(settings),
        emitter,
        install_stop(),
    )
    try:
        await worker.run()
    finally:
        await emitter.aclose()
        await http.aclose()
        await r.aclose()
        await engine.dispose()
