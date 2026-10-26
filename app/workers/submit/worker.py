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
    QueueEntry,
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
)
from app.services.storage import get_incoming_client, get_object
from app.telemetry import job_span, setup_telemetry
from app.vocabulary import FailureStage, JobEventName, JobStatus
from app.workers.shutdown import install_stop
from app.workers.stages import StageTimer, log_stages, stage_metric_rows
from app.workers.submit.steps import (
    allocate_scored,
    classify_dispatch_error,
    dispatch_event_fields,
    failed_dispatch_attempt,
    pending_job,
    retried_dispatch_attempt,
    serves,
    unstored_capacity,
)
from app.workers.submit.types import BackendSlot, DispatchFailure, PendingJob

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
        # and cancelled when the worker stops; gated per backend so concurrent rebuilds
        # of the same backend's domain set can't race.
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
            if not t.cancelled() and (error := t.exception()) is not None:
                logger.error("Domain sync for %s failed", backend.url, exc_info=error)

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

    async def dispatch_job(
        self,
        job_id: str,
        original_score: float,
        backend: Backend,
        backend_domains: dict[str, frozenset[str]],
        state: JobState,
    ) -> None:
        # Skip if this backend does not serve the job's domain + outputs. Checked against
        # the capabilities loaded once per tick (no per-job DB round-trip); the job goes
        # back on the queue for a backend that does serve it.
        if not serves(backend_domains, state.domain, state.outputs):
            await requeue_job(self.r, job_id, original_score)
            return

        # Read image from MinIO — keyed by job_id (the server PK), matching the submit upload.
        object_path = f"{state.username}/{job_id}{state.ext}"
        try:
            with self.timer.stage("image_get"):
                image_bytes = await get_object(
                    self.incoming_client, self.settings.minio_incoming_bucket, object_path
                )
        except Exception as e:
            logger.exception("Failed to read image for job %s", job_id)
            msg = f"Failed to read image: {describe_exception(e)}"
            attempt = failed_dispatch_attempt(backend.id, None, msg)
            await self._fail_job(
                job_id, state, backend, attempt, failure(FailureStage.DISPATCH, None)
            )
            return

        try:
            await self._submit(job_id, state, backend, image_bytes)
        except EngineFullError:
            logger.info(f"Engine {backend.url} full, requeuing job {job_id}")
            await requeue_job(self.r, job_id, original_score)
        except Exception as e:
            await self._dispatch_failed(job_id, original_score, state, backend, e)

    async def _submit(
        self, job_id: str, state: JobState, backend: Backend, image_bytes: bytes
    ) -> None:
        """Hand the image to the engine and record the job as running there."""
        adapter = self.adapters.for_backend(backend, backend.url)
        # Named by job id: the external id is the caller's free text, not a file name.
        filename = f"{job_id}{state.ext}"
        with self.timer.stage("engine_submit"):
            engine_job_id = await adapter.submit(
                image_bytes, filename, state.domain, state.outputs, state.options
            )
        logger.info(f"Job {job_id} running, engine_job_id={engine_job_id}")
        with self.timer.stage("engine_version"):
            engine_version = await adapter.version()

        async with self.session_factory() as db:
            state_ttl = await config_service.get_state_ttl_seconds(db)
        try:
            with self.timer.stage("set_running"):
                await set_running(self.r, job_id, engine_job_id, backend.url, backend.id, state_ttl)
        except Exception:
            # Nothing tracks the engine's job now: name it, so it can be found there.
            logger.error(
                "Job %s was accepted by %s as %s but not recorded as running",
                job_id,
                backend.url,
                engine_job_id,
            )
            raise
        await self._record_running(job_id, backend, engine_job_id, engine_version)
        self.emitter.emit(
            self._event(
                JobEventName.DISPATCHED,
                job_id,
                state,
                backend,
                engine_version=engine_version or "",
                queue_s=max(time.time() - state.submitted_at, 0.0),
            )
        )

    async def _record_running(
        self, job_id: str, backend: Backend, engine_job_id: str, engine_version: str | None
    ) -> None:
        """Mirror the dispatch on the job's row. Redis already has the job running, so the
        poller harvests it whatever happens here: a row left queued is marked done by the
        harvest. Failing the job instead would throw away the engine's work."""
        try:
            async with self.session_factory() as db:
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
        except Exception:
            logger.exception(
                "Job %s runs on %s as %s, but its row wasn't updated; the harvest will",
                job_id,
                backend.url,
                engine_job_id,
            )

    async def _dispatch_failed(
        self,
        job_id: str,
        original_score: float,
        state: JobState,
        backend: Backend,
        error: Exception,
    ) -> None:
        """Retry a transient failure while the budget lasts; fail the job otherwise.
        Called from the ``except`` block, so a terminal failure's traceback is logged."""
        failed = classify_dispatch_error(error)
        async with self.session_factory() as db:
            max_requeues = (await config_service.get_runtime_config(db)).max_requeues
        if failed.transient and state.requeues < max_requeues:
            await self._retry_later(job_id, original_score, state, backend, failed, max_requeues)
            return

        # Terminal: not transient, or the retry budget is spent.
        msg = failed.error
        if failed.transient:
            logger.error(
                "Dispatch for job %s failed after %d requeue attempts: %s",
                job_id,
                max_requeues,
                msg,
            )
            msg = f"{msg} (exceeded {max_requeues} requeue attempts)"
        else:
            logger.exception("Failed to dispatch job %s", job_id)
        if perf.counts_against_backend(failed.http_status):
            await perf.record_failure(self.r, backend.id, state.domain)
        attempt = failed_dispatch_attempt(backend.id, failed.http_status, msg)
        await self._fail_job(job_id, state, backend, attempt, dispatch_event_fields(failed))

    async def _retry_later(
        self,
        job_id: str,
        original_score: float,
        state: JobState,
        backend: Backend,
        failed: DispatchFailure,
        max_requeues: int,
    ) -> None:
        """A transient failure (backend down/overloaded) with budget left: sideline this
        backend for the health-cache window so the retry lands elsewhere, bump the
        requeue counter the budget reads (Redis + DB), and schedule a delayed retry."""
        delay = backoff_seconds(state.requeues)
        logger.warning(
            "Transient dispatch error for job %s on %s (attempt %d/%d), requeue in %.1fs: %s",
            job_id,
            backend.url,
            state.requeues + 1,
            max_requeues,
            delay,
            failed.error,
        )
        self.health_cache[backend.id] = (False, time.time())
        await count_requeue(self.r, job_id)
        await schedule_retry(self.r, job_id, original_score, delay, avoid_backend_id=backend.id)
        await perf.record_failure(self.r, backend.id, state.domain)
        attempt = retried_dispatch_attempt(backend.id, failed.http_status, failed.error)
        async with self.session_factory() as db:
            await db.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(requeues=Job.requeues + 1, **append_attempt(attempt))
            )
            await db.commit()
        self.emitter.emit(
            self._event(
                JobEventName.RETRIED,
                job_id,
                state,
                backend,
                **dispatch_event_fields(failed),
            )
        )

    async def _fail_job(
        self,
        job_id: str,
        state: JobState,
        backend: Backend,
        attempt: Attempt,
        event_fields: dict[str, Any],
    ) -> None:
        """Fail the job for good at dispatch, in Redis and its row, and report it."""
        msg = attempt.error
        async with self.session_factory() as db:
            state_ttl = await config_service.get_state_ttl_seconds(db)
            batch = await set_failed(self.r, job_id, msg, state_ttl)
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
        self.emitter.emit(
            self._event(
                JobEventName.FAILED,
                job_id,
                state,
                backend,
                **event_fields,
            )
        )
        payload = job_event_payload(
            job_id, state.external_id, JobStatus.FAILED, batch_id=state.batch_id, error=msg
        )
        await publish_event(self.r, state.username, payload)
        if batch is not None:
            await publish_event(self.r, state.username, batch_event(batch))

    def _event(
        self, name: JobEventName, job_id: str, state: JobState, backend: Backend, **fields: Any
    ) -> JobEvent:
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

    async def run(self) -> None:
        """Dispatch, then wait for work or a freed slot, until asked to stop."""
        logger.info("Submit worker started")
        try:
            while not self.stop.is_set():
                try:
                    await self.run_tick()
                except Exception:
                    logger.exception("Submit worker failed")
                await self._flush_stages()
                await self._wait()
        finally:
            await self._cancel_domain_syncs()

    async def _cancel_domain_syncs(self) -> None:
        """A sync is best-effort and redone on the next start: cancel, don't wait."""
        for task in self.sync_tasks:
            task.cancel()
        await asyncio.gather(*self.sync_tasks, return_exceptions=True)

    async def run_tick(self) -> None:
        """Dispatch as many queued jobs as the healthy backends and the storage budget
        allow, tier by tier."""
        with self.timer.stage("refresh"):
            await self.refresh_backends()
        await self._promote_retries()
        # Health checks run even while dispatch is paused: they also start domain syncs.
        eligible = await self._eligible_slots()
        remaining = await self._dispatch_budget()
        if remaining == 0:
            return

        # Strict priority tiers: a higher-priority tier drains first; within a tier
        # spread jobs round-robin so no single backend fills before its peers.
        for priority in sorted({slot.backend.priority for slot in eligible}, reverse=True):
            tier = [slot for slot in eligible if slot.backend.priority == priority]
            capacity = min(sum(slot.free for slot in tier), remaining)
            if capacity == 0:
                continue  # this tier is full; a lower one may still have room
            # Peeked, not popped: a job leaves its queue only once claimed, so one no
            # backend serves yet, or one in hand when this worker dies, stays queued.
            with self.timer.stage("peek"):
                entries = await peek_pending(self.r, capacity)
            if not entries:
                return  # nothing pending — lower tiers have nothing to do either
            remaining -= await self._dispatch_tier(tier, priority, entries)

    async def _promote_retries(self) -> None:
        """Promote delayed retries that are now due."""
        with self.timer.stage("promote"):
            promoted = await promote_due_retries(self.r, time.time())
        if promoted:
            logger.info(f"Promoted {promoted} delayed retry/requeue(s)")

    async def _eligible_slots(self) -> list[BackendSlot]:
        """This tick's eligible backends: healthy, with their free capacity + served
        domains (each looked up once). Full ones stay in (free=0) so a retry waits for
        them rather than falling back to the backend it avoids."""
        # At once: a backend that's down takes its whole health timeout to say so.
        with self.timer.stage("health"):
            health = await asyncio.gather(*(self.check_health(b) for b in self.backends))
        healthy_backends = [
            backend for backend, healthy in zip(self.backends, health, strict=True) if healthy
        ]
        with self.timer.stage("backend_state"):
            backend_states = await get_backends_state(
                self.r, [backend.id for backend in healthy_backends]
            )
        eligible: list[BackendSlot] = []
        for backend in healthy_backends:
            inflight, batches = backend_states[backend.id]
            eligible.append(
                BackendSlot(
                    backend,
                    max(backend.max_inflight - inflight, 0),
                    await self.served_domains(backend.id),
                    inflight=inflight,
                    batches=batches,
                )
            )
        return eligible

    async def _dispatch_budget(self) -> int:
        """How many jobs may still be dispatched before results waiting to be stored hit
        the limit. In-flight engine work will also need storage, so it counts against
        the same budget as jobs already waiting for harvest."""
        inflight_depth, harvest_depth = await unstored_depth(self.r)
        limit = self.settings.unstored_job_max
        remaining = unstored_capacity(limit, inflight_depth, harvest_depth)
        if remaining == 0 and not self.paused_for_harvest:
            logger.warning(
                "Unstored jobs at limit (running=%d, storing=%d, limit=%d); pausing dispatch",
                inflight_depth,
                harvest_depth,
                limit,
            )
            self.paused_for_harvest = True
        elif remaining > 0 and self.paused_for_harvest:
            logger.info("Unstored jobs below limit; resuming dispatch")
            self.paused_for_harvest = False
        return remaining

    async def _dispatch_tier(
        self, tier: list[BackendSlot], priority: int, entries: list[QueueEntry]
    ) -> int:
        """Allocate the peeked jobs over the tier's backends, claim and dispatch them;
        how many were dispatched. Leftovers stay queued for a lower tier or a backend
        that frees up."""
        states = await self._queued_states(entries)
        if not states:
            return 0

        pending = [
            pending_job(entry, states[entry.job_id]) for entry in entries if entry.job_id in states
        ]
        allocated = await self._allocate(tier, pending)
        by_id = {entry.job_id: entry for entry in entries}
        # Another submit worker (two run during a rollout) may have claimed a job first;
        # it's dispatched there, not twice.
        with self.timer.stage("claim"):
            claimed = await claim_pending(self.r, [by_id[job.job_id] for job, _ in allocated])
        assignments = [(job, slot) for job, slot in allocated if job.job_id in claimed]
        if not assignments:
            return 0

        logger.info(
            f"Dispatching {len(assignments)} job(s) across {len(tier)} backend(s) "
            f"at priority {priority}"
        )
        with self.timer.stage("dispatch"):
            results = await asyncio.gather(
                *[
                    self.dispatch(
                        job.job_id, job.score, slot.backend, slot.domains, states[job.job_id]
                    )
                    for job, slot in assignments
                ],
                return_exceptions=True,
            )
        for result in results:
            if isinstance(result, Exception):
                logger.error("Dispatch failed: %s", result)
        return len(assignments)

    async def _queued_states(self, entries: list[QueueEntry]) -> dict[str, JobState]:
        """The peeked jobs' states. A job whose state expired or was deleted can't be
        dispatched; left queued it would take a peek slot every tick, so it's dropped."""
        with self.timer.stage("meta"):
            found = await get_jobs(self.r, [entry.job_id for entry in entries])
        gone = [entry for entry in entries if found[entry.job_id] is None]
        if gone:
            await drop_pending(self.r, gone)
            logger.warning(
                "Dropped %d queued job(s) whose state is gone: %s",
                len(gone),
                [entry.job_id for entry in gone],
            )
        return {job_id: state for job_id, state in found.items() if state is not None}

    async def _allocate(
        self, tier: list[BackendSlot], pending: list[PendingJob]
    ) -> list[tuple[PendingJob, BackendSlot]]:
        """Give each job to the backend that would finish it first."""
        # Speed scores for this tier's backends and these jobs' domains, once.
        job_domains = {job.domain for job in pending}
        with self.timer.stage("scores"):
            scores = await perf.load_scores(self.r, [slot.backend.id for slot in tier], job_domains)
            fallback = await _fallback_spp(self.r, job_domains)
            async with self.session_factory() as db:
                tolerance = (await config_service.get_runtime_config(db)).affinity_tolerance

        def spp(backend_id: int, domain: str) -> float:
            return perf.effective_spp(backend_id, domain, scores, fallback[domain])

        with self.timer.stage("allocate"):
            assignments, _ = allocate_scored(tier, pending, spp, tolerance)
        return assignments

    async def _flush_stages(self) -> None:
        """Log the stage timings, and send them to ClickHouse, once a minute."""
        if time.monotonic() - self.stages_flushed_at < 60:
            return

        self.stages_flushed_at = time.monotonic()
        p95 = self.timer.p95()
        totals = self.timer.flush()
        log_stages(logger, totals, p95)
        if not (self.settings.clickhouse_url and totals):
            return
        try:
            await live_metrics.insert_rows(
                self.http, self.settings, stage_metric_rows("submit", totals)
            )
        except Exception as exc:
            logger.warning("Stage metrics dropped: %s", exc)

    async def _wait(self) -> None:
        """Block until new work / a freed slot signals (signal_submit), with the tick as
        an idle-wait floor. Event-driven so fast jobs refill slots immediately and slow
        jobs don't busy-poll."""
        try:
            await wait_for_submit_signal(self.r, self.settings.submit_tick_seconds)
        except Exception:  # a Redis hiccup must not end the worker
            logger.exception("Waiting for the submit signal failed")
            await asyncio.sleep(self.settings.submit_tick_seconds)


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
