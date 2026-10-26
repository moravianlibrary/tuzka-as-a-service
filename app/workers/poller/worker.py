"""Poller worker: polls engines for in-flight job results, stores output, and finalizes jobs."""

import asyncio
import logging
import random
import time
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import redis.asyncio as aioredis
import zstandard
from miniopy_async.api import Minio
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import utcnow
from app.config import Settings
from app.engines.base import EngineAdapter, OutputUnavailable
from app.engines.cache import AdapterCache
from app.engines.outputs import ext_for
from app.models.backend import Backend
from app.models.db import make_engine, make_session_factory
from app.models.job import Job, JobResult
from app.schemas.attempt import Attempt
from app.schemas.redis import BatchCounts, EngineRun, JobState
from app.services import batch_store, live_metrics, perf
from app.services import config as config_service
from app.services.alto_stats import parse_alto
from app.services.event_emitter import EventEmitter, get_emitter
from app.services.events import (
    JobEvent,
    attempt_failure,
    durations,
    extra_metrics,
)
from app.services.job_attempts import append_attempt
from app.services.redis_jobs import (
    batch_event,
    count_requeue,
    due_harvest_ids,
    forget_harvests,
    get_inflight_ids,
    get_jobs,
    job_event_payload,
    next_harvest_at,
    publish_event,
    record_page_seconds,
    release_and_requeue,
    release_done,
    reschedule_harvest,
    schedule_next_poll,
    set_done,
    set_failed,
    signal_poll,
    wait_for_poll_signal,
)
from app.services.redis_pool import make_redis
from app.services.retry_policy import (
    backoff_seconds,
    describe_exception,
    http_status_of,
    is_retryable_engine_failure,
    is_retryable_poll_error,
)
from app.services.storage import (
    get_results_client,
    get_results_public_client,
    presign_get,
    put_object,
)
from app.telemetry import job_span, setup_telemetry
from app.vocabulary import (
    EngineState,
    FailureStage,
    JobEventName,
    JobStatus,
    PollStatus,
)
from app.workers.poller.steps import (
    drain_poll_queue,
    engine_run,
    engine_stamps,
    fetch_outputs,
    is_transient_harvest_error,
    next_poll_delay,
    next_poll_timeout,
    output_stats,
    plan_polls,
    probe_failure_status,
    require_engine_job_id,
)
from app.workers.poller.types import (
    FailedPoll,
    HarvestContext,
    OutputStats,
    PollOutcome,
    RequeuedPoll,
    StoredHarvest,
)
from app.workers.shutdown import install_stop
from app.workers.stages import StageTimer, log_stages, stage_metric_rows

logger = logging.getLogger("poller-worker")


class PollerWorker:
    """The poller's long-lived clients and caches, and the steps of a pass."""

    def __init__(
        self,
        settings: Settings,
        r: aioredis.Redis,
        session_factory: async_sessionmaker[AsyncSession],
        http: httpx.AsyncClient,
        results_client: Minio,
        results_public_client: Minio,
        emitter: EventEmitter,
        stop: asyncio.Event,
    ) -> None:
        self.settings = settings
        self.r = r
        self.session_factory = session_factory
        self.http = http
        self.results_client = results_client
        self.results_public_client = results_public_client
        self.emitter = emitter
        self.stop = stop
        self.timer = StageTimer()
        self.stages_flushed_at = time.monotonic()
        # Backend rows by id, read from the DB once each; None = the row is gone.
        self.backends: dict[int, Backend | None] = {}
        # Keyed by the job hash's URL, i.e. where the job was actually dispatched, so a
        # backend re-registered at a new URL still gets its in-flight jobs polled.
        self.adapters = AdapterCache(http, settings.key_encryption_secret)
        self.harvest_sem = asyncio.Semaphore(settings.poller_harvest_concurrency)
        # Bounds a pass's status checks: one per due job, so hundreds at once otherwise,
        # each holding an engine and a Redis connection.
        self.poll_sem = asyncio.Semaphore(settings.poller_poll_concurrency)
        self.released_since_harvest = 0
        self.rng = random.Random()  # the requeues' backoff jitter
        self.poll_task: asyncio.Task[None] | None = None

    async def backend_for(self, state: JobState) -> Backend | None:
        backend_id = state.backend_id
        if backend_id is None:
            return None
        if backend_id not in self.backends:
            async with self.session_factory() as db:
                self.backends[backend_id] = await db.get(Backend, backend_id)
        return self.backends[backend_id]

    async def adapter_for(self, state: JobState) -> EngineAdapter:
        url = state.backend_url
        if url is None:
            raise LookupError(f"job state has no backend_url (status {state.status})")
        return self.adapters.for_backend(await self.backend_for(state), url)

    def job_event(
        self, name: JobEventName, job_id: str, state: JobState, job_rec: Job | None, **fields: Any
    ) -> JobEvent:
        """An event for a job the poller is finishing, from its hash and DB row."""
        backend = self.backends.get(state.backend_id) if state.backend_id is not None else None
        return JobEvent.from_state(
            utcnow(),
            name,
            job_id,
            state,
            backend=backend.display_label if backend else "",
            adapter=backend.adapter if backend else "",
            engine_version=(job_rec.engine_version if job_rec else None) or "",
            file_size_bytes=job_rec.file_size_bytes if job_rec else None,
            **fields,
        )

    async def check_one(self, job_id: str, state: JobState) -> PollOutcome:
        try:
            adapter = await self.adapter_for(state)
            engine_job_id = require_engine_job_id(state)
            status_resp = await adapter.status(engine_job_id)
        except httpx.TransportError as e:
            logger.warning(f"Engine unreachable for job {job_id}: {e}")
            return PollOutcome(
                job_id,
                PollStatus.UNREACHABLE,
                state,
                EngineRun(),
                retryable=True,
                http_status=None,
                error=describe_exception(e),
            )
        except Exception as e:
            logger.error(f"Failed to check status for job {job_id}: {e}")
            return PollOutcome(
                job_id,
                PollStatus.ERROR,
                state,
                EngineRun(),
                retryable=is_retryable_poll_error(e),
                http_status=http_status_of(e),
                error=describe_exception(e),
            )

        if status_resp.state not in (EngineState.FAILED, EngineState.ERROR):
            return PollOutcome(
                job_id,
                PollStatus(status_resp.state),
                state,
                engine_run(status_resp),
                retryable=False,
                http_status=None,
                error=None,
            )
        # Engine-reported failure: error_status decides retry vs fail. Engines that
        # predate the field still expose it as the result endpoint's HTTP status (D3).
        error_status = status_resp.error_status
        if error_status is None:
            error_status = await probe_failure_status(adapter, engine_job_id)
        return PollOutcome(
            job_id,
            PollStatus(status_resp.state),
            state,
            engine_run(status_resp),
            retryable=is_retryable_engine_failure(error_status),
            http_status=error_status,
            error=status_resp.error,
            stage=FailureStage.ENGINE,
        )

    async def harvest(self, job_id: str, state: JobState, run: EngineRun | None) -> None:
        # The job was moved to the harvest queue at engine-done, together with the last
        # engine status, so a retry (or a pass after a poller restart) still has the
        # run's timestamps even though the poll outcome is long gone.
        if run is None:
            run = state.harvest_times or EngineRun()
        try:
            await self._harvest(job_id, state, run)
        except OutputUnavailable as e:
            # The engine can't produce an output this job asked for (TuzkaM: georeferencing
            # failed). Retrying gives the same answer, so the job fails for good.
            logger.warning("Job %s: %s", job_id, e)
            attempt = Attempt(
                at=utcnow(),
                stage=FailureStage.HARVEST,
                backend_id=state.backend_id,
                http_status=409,
                error=str(e),
                retry=False,
            )
            await self.mark_failed(job_id, state, str(e), attempt=attempt, run=run)
        except Exception as e:
            await self._harvest_failed(job_id, state, run, e)

    async def _harvest_failed(
        self, job_id: str, state: JobState, run: EngineRun, error: Exception
    ) -> None:
        """The engine already finished this job; a transient DB/storage error here is not
        the job's fault. Keep it on the durable harvest queue and retry with backoff
        rather than stamping a raw infrastructure traceback as a permanent user-visible
        failure. Called from the ``except`` block, so the traceback is still logged."""
        if is_transient_harvest_error(error):
            delay = backoff_seconds(state.harvest_attempts + 1)
            await reschedule_harvest(self.r, job_id, delay)
            logger.warning(
                "Transient error harvesting job %s; retrying in %.1fs: %s", job_id, delay, error
            )
            return

        logger.exception("Failed to harvest job %s", job_id)
        await self.mark_failed(job_id, state, str(error), run=run)

    async def _harvest(
        self,
        job_id: str,
        state: JobState,
        run: EngineRun,
    ) -> None:
        """Fetch, store and record the job's outputs, then report it done (once)."""
        engine_job_id = require_engine_job_id(state)
        adapter = await self.adapter_for(state)
        with self.timer.stage("fetch_outputs"):
            fetched = await fetch_outputs(adapter, engine_job_id, state.outputs)
        stats = await self._output_stats(fetched, run)
        stored = await self._store_results(job_id, state, run, fetched)

        with self.timer.stage("set_done"):
            batch = await set_done(self.r, job_id, stored.state_ttl, state)  # idempotent
        if not stored.first_harvest:
            logger.info("Job %s was already harvested; not reporting it again", job_id)
            if batch is not None:  # the first harvest died before its set_done
                await publish_event(self.r, state.username, batch_event(batch))
            return

        await self._record_run_time(state, stored)
        with self.timer.stage("events"):
            await self._report_done(job_id, state, run, stats, stored, batch)
        logger.info(f"Published done event for {state.username}")

    async def _output_stats(self, fetched: dict[str, bytes], run: EngineRun) -> OutputStats:
        parsed_alto = None
        # Blocks are ALTO-only, so parse those (present for multi/alto) before compression.
        # In a thread: a page's ALTO is megabytes of XML, and the loop serves every poll.
        if alto := fetched.get("alto"):
            with self.timer.stage("parse_alto"):
                parsed_alto = await asyncio.to_thread(parse_alto, alto)
        return output_stats(run, parsed_alto, fetched.get("txt"))

    async def _store_results(
        self, job_id: str, state: JobState, run: EngineRun, fetched: dict[str, bytes]
    ) -> StoredHarvest:
        """Store every output, record them and mark the job done. Only the first harvest
        marks it done; a second one (another poller got there first) must not report the
        job, its events or its timing again. No DB connection is held while storing: one
        per concurrent harvest, idle for the uploads, would drain the pool."""
        context = await self._harvest_context(job_id)
        # Each output is its own object, so they're stored at once (one per format).
        result_rows = await asyncio.gather(
            *(
                self._store_output(
                    job_id, state.username, output, raw, context.presigned_ttl_minutes
                )
                for output, raw in fetched.items()
            )
        )

        stored_at = utcnow()
        stamps = engine_stamps(run)
        finished_at: datetime = stamps.get("finished_at") or utcnow()
        done_values: dict[str, str | datetime] = {
            "status": JobStatus.DONE,
            "stored_at": stored_at,
            **stamps,
            "finished_at": finished_at,
        }
        first_harvest = await self._record_done(job_id, state.batch_id, result_rows, done_values)

        return StoredHarvest(
            first_harvest=first_harvest,
            job=context.job,
            device=context.device,
            state_ttl=context.state_ttl,
            urls={row["fmt"]: row["presigned_url"] for row in result_rows},
            engine_received_at=stamps.get("engine_received_at"),
            started_at=stamps.get("started_at"),
            engine_finished_at=stamps.get("finished_at"),
            finished_at=finished_at,
            stored_at=stored_at,
        )

    async def _harvest_context(self, job_id: str) -> HarvestContext:
        """What storing a job's results needs from the DB, read in one short session."""
        async with self.session_factory() as db:
            # The raw job, for submitted_at, engine_version, device and file_size.
            job_row = await db.execute(
                select(Job, Backend.device)
                .outerjoin(Backend, Job.backend_id == Backend.id)
                .where(Job.id == job_id)
            )
            job_rec, backend_device = job_row.tuples().first() or (None, None)
            config = await config_service.get_runtime_config(db)
            state_ttl = await config_service.get_state_ttl_seconds(db)
        return HarvestContext(
            job=job_rec,
            device=backend_device or "",
            presigned_ttl_minutes=config.presigned_ttl_minutes,
            state_ttl=state_ttl,
        )

    async def _record_done(
        self,
        job_id: str,
        batch_id: str | None,
        result_rows: Sequence[dict[str, Any]],
        done_values: dict[str, str | datetime],
    ) -> bool:
        """Record the stored outputs and mark the job done, in one transaction; whether
        this harvest was the first to mark it."""
        async with self.session_factory() as db:
            with self.timer.stage("db_write"):
                if result_rows:
                    # One statement for every output: with the job update below, a two-output
                    # job costs two round trips instead of three. An upsert, because a
                    # re-harvest rewrote the same objects.
                    stmt = pg_insert(JobResult).values(result_rows)
                    await db.execute(
                        stmt.on_conflict_do_update(
                            index_elements=["job_id", "fmt"],
                            set_={
                                "presigned_url": stmt.excluded.presigned_url,
                                "presigned_until": stmt.excluded.presigned_until,
                                "size_bytes": stmt.excluded.size_bytes,
                                "evicted_at": stmt.excluded.evicted_at,
                            },
                        )
                    )
                claimed = await db.execute(
                    update(Job)
                    .where(Job.id == job_id, Job.status != JobStatus.DONE)
                    .values(**done_values)
                )
                await batch_store.finish_if_done(db, batch_id)
                await db.commit()
        return getattr(claimed, "rowcount", 1) == 1

    async def _store_output(
        self, job_id: str, username: str, output: str, raw: bytes, presigned_ttl: int
    ) -> dict[str, Any]:
        """Compress and store one output; its ``job_results`` row."""
        obj_path = f"{username}/{job_id}.{ext_for(output)}.zst"
        with self.timer.stage("compress"):
            compressed = await asyncio.to_thread(
                _compress, raw, self.settings.zstd_compression_level
            )
        with self.timer.stage("store_put"):
            await put_object(
                self.results_client,
                self.settings.minio_results_bucket,
                obj_path,
                compressed,
                "application/octet-stream",
            )
        logger.info(f"Stored {obj_path} ({len(compressed)} bytes)")

        with self.timer.stage("presign"):
            presigned_url = await presign_get(
                self.results_public_client,
                self.settings.minio_results_bucket,
                obj_path,
                presigned_ttl,
            )
        return {
            "job_id": job_id,
            "fmt": output,
            "presigned_url": presigned_url,
            "presigned_until": utcnow() + timedelta(minutes=presigned_ttl),
            "size_bytes": len(compressed),  # what the storage cap counts
            "evicted_at": None,
        }

    async def _record_run_time(self, state: JobState, stored: StoredHarvest) -> None:
        """Engine-clock run time per megapixel scores this backend for dispatch, and feeds
        /load's seconds-per-page estimate."""
        seconds = stored.run_seconds
        if seconds is None or seconds <= 0:
            return

        if state.backend_id is not None:
            await perf.record_service(
                self.r, state.backend_id, state.domain, seconds, state.megapixels
            )
        await record_page_seconds(self.r, state.domain, seconds)

    async def _report_done(
        self,
        job_id: str,
        state: JobState,
        run: EngineRun,
        stats: OutputStats,
        stored: StoredHarvest,
        batch: BatchCounts | None,
    ) -> None:
        job = stored.job
        self.emitter.emit(
            self.job_event(
                JobEventName.DONE,
                job_id,
                state,
                job,
                device=stored.device,
                lines=stats.lines,
                blocks=stats.blocks,
                chars=stats.chars,
                mean_conf=stats.mean_conf,
                extra=extra_metrics(run.metrics),
                **durations(
                    job.submitted_at if job else None,
                    job.dispatched_at if job else None,
                    stored.engine_received_at,
                    stored.started_at,
                    stored.finished_at,
                    stored.stored_at,
                ),
            ),
        )
        urls = {f"{output}_url": url for output, url in stored.urls.items()}  # alto_url, …
        payload = job_event_payload(
            job_id, state.external_id, JobStatus.DONE, batch_id=state.batch_id, **urls
        )
        await publish_event(self.r, state.username, payload)
        if batch is not None:
            await publish_event(self.r, state.username, batch_event(batch))

    async def mark_failed(
        self,
        job_id: str,
        state: JobState,
        error: str,
        attempt: Attempt | None = None,
        run: EngineRun | None = None,
    ) -> None:
        """Fail the job for good. ``run`` is the engine's last status, when it ran the
        job: its stamps are kept, so a job that failed after minutes of work shows them."""
        username = state.username
        external_id = state.external_id
        failed_at = utcnow()
        async with self.session_factory() as db:
            state_ttl = await config_service.get_state_ttl_seconds(db)
            # A job another harvest already finished stays done: a late failure (say a
            # second harvest that lost the race) mustn't overwrite its results.
            done = await db.scalar(select(Job.status).where(Job.id == job_id)) == JobStatus.DONE
            if done:
                logger.warning("Job %s is already done; not failing it: %s", job_id, error)
                await set_done(self.r, job_id, state_ttl, state)
                return
            batch = await set_failed(self.r, job_id, error, state_ttl)
            if (
                attempt
                and attempt.backend_id is not None
                and perf.counts_against_backend(attempt.http_status)
            ):
                await perf.record_failure(self.r, attempt.backend_id, state.domain)
            values: dict[str, Any] = {
                "status": JobStatus.FAILED,
                "error": error,
                "finished_at": failed_at,
                **(engine_stamps(run) if run else {}),
            }
            finished_at = values["finished_at"]
            if attempt is not None:
                values.update(append_attempt(attempt))
            await db.execute(
                update(Job).where(Job.id == job_id, Job.status != JobStatus.DONE).values(**values)
            )

            # The job as it ended, for its failed event.
            job_row = await db.execute(
                select(Job, Backend.device)
                .outerjoin(Backend, Job.backend_id == Backend.id)
                .where(Job.id == job_id)
            )
            job_rec, backend_device = job_row.first() or (None, None)
            await batch_store.finish_if_done(db, state.batch_id)

            await db.commit()
        self.emitter.emit(
            self.job_event(
                JobEventName.FAILED,
                job_id,
                state,
                job_rec,
                device=backend_device or "",
                **attempt_failure(attempt),
                **durations(
                    job_rec.submitted_at if job_rec else None,
                    job_rec.dispatched_at if job_rec else None,
                    job_rec.engine_received_at if job_rec else None,
                    job_rec.started_at if job_rec else None,
                    finished_at,
                    None,
                ),
            )
        )
        await publish_event(
            self.r,
            username,
            job_event_payload(
                job_id, external_id, JobStatus.FAILED, batch_id=state.batch_id, error=error
            ),
        )
        if batch is not None:
            await publish_event(self.r, username, batch_event(batch))

    async def check_with_sem(self, job_id: str, state: JobState) -> PollOutcome:
        with (
            job_span("poll", job_id, state, backend_id=state.backend_id),
            self.timer.stage("poll"),
        ):
            with self.timer.stage("poll_wait"):
                await self.poll_sem.acquire()
            try:
                return await self.check_one(job_id, state)
            finally:
                self.poll_sem.release()

    async def harvest_with_sem(self, job_id: str, state: JobState, run: EngineRun | None) -> None:
        with self.timer.stage("harvest_wait"):
            await self.harvest_sem.acquire()
        try:
            with job_span("harvest", job_id, state, backend_id=state.backend_id):
                await self.harvest(job_id, state, run)
        finally:
            self.harvest_sem.release()

    async def process_chunk(
        self, statuses: list[PollOutcome], max_requeues: int, state_ttl: int
    ) -> None:
        with self.timer.stage("classify"):
            plan = plan_polls(statuses, max_requeues, utcnow(), self.rng)
        if plan.done:
            await self._release_for_harvest(plan.done)
        for running in plan.running:
            await self._schedule_next_poll(running)
        for failed in plan.failed:
            await self._fail_polled(failed)
        if plan.requeued:
            await self._requeue(plan.requeued, state_ttl)
        if plan.done:
            self.released_since_harvest += len(plan.done)
            await signal_poll(self.r)

    async def _release_for_harvest(self, done: list[PollOutcome]) -> None:
        """The engine is done: free its slot now and queue the harvest, rather than
        holding the slot through fetch/store/DB. The harvest drain picks these up in this
        same pass when it has capacity."""
        released = await asyncio.gather(
            *[release_done(self.r, o.job_id, o.state, o.run) for o in done],
            return_exceptions=True,
        )
        storing_ids: list[UUID] = []
        for outcome, release_result in zip(done, released, strict=True):
            if isinstance(release_result, Exception):
                logger.error(
                    "Releasing job %s for harvest failed: %s", outcome.job_id, release_result
                )
            elif release_result:
                storing_ids.append(UUID(outcome.job_id))
        if not storing_ids:
            return

        # Redis owns durable harvest work before Postgres exposes `storing`.
        async with self.session_factory() as db:
            await db.execute(
                update(Job)
                .where(Job.id.in_(storing_ids), Job.status == JobStatus.RUNNING)
                .values(status=JobStatus.STORING)
            )
            await db.commit()

    async def _schedule_next_poll(self, running: PollOutcome) -> None:
        """Back off the next status check, so the wait fires right when the soonest job
        is due."""
        now = time.time()
        delay = next_poll_delay(
            running.state, now, self.settings.poll_backoff_initial, self.settings.poll_backoff_max
        )
        await schedule_next_poll(self.r, running.job_id, now + delay, now)

    async def _fail_polled(self, failed: FailedPoll) -> None:
        job_id, state = failed.outcome.job_id, failed.outcome.state
        with (
            job_span("fail", job_id, state, backend_id=state.backend_id),
            self.timer.stage("fail"),
        ):
            await self.mark_failed(
                job_id, state, failed.error, attempt=failed.attempt, run=failed.outcome.run
            )

    async def _requeue(self, requeued: list[RequeuedPoll], state_ttl: int) -> None:
        """Re-queue retryable failures (unreachable pod, engine 5xx, lost job) so another
        backend picks them up after a jittered backoff. Redis is updated first (release
        slot + schedule the retry + bump the requeue counter the budget reads); the DB
        mirrors it, with the attempt recorded, in one batched session."""
        for requeue in requeued:
            await self._requeue_in_redis(requeue, state_ttl)
        async with self.session_factory() as db:
            for requeue in requeued:
                await db.execute(
                    update(Job)
                    .where(Job.id == requeue.outcome.job_id)
                    .values(
                        status=JobStatus.QUEUED,
                        engine_job_id=None,
                        backend_id=None,
                        dispatched_at=None,
                        requeues=Job.requeues + 1,
                        **append_attempt(requeue.attempt),
                    )
                )
            await db.commit()
        for requeue in requeued:
            outcome = requeue.outcome
            self.emitter.emit(
                self.job_event(
                    JobEventName.RETRIED,
                    outcome.job_id,
                    outcome.state,
                    None,
                    **attempt_failure(requeue.attempt),
                )
            )

    async def _requeue_in_redis(self, requeue: RequeuedPoll, state_ttl: int) -> None:
        job_id, state = requeue.outcome.job_id, requeue.outcome.state
        avoid_id = state.backend_id
        with self.timer.stage("requeue"):
            await release_and_requeue(
                self.r,
                job_id,
                state.submitted_at,
                state_ttl,
                delay_s=requeue.delay_s,
                avoid_backend_id=avoid_id,
            )
            await count_requeue(self.r, job_id)
            if avoid_id is not None:  # retryable: the backend's fault
                await perf.record_failure(self.r, avoid_id, state.domain)
        logger.info(
            f"Re-queued job {job_id} in {requeue.delay_s:.1f}s (avoiding backend {avoid_id})"
        )

    async def drain_due(self, due: list[str], states: dict[str, JobState]) -> None:
        # Refill each free status slot while the main loop harvests in parallel.
        async with self.session_factory() as db:
            max_requeues = (await config_service.get_runtime_config(db)).max_requeues
            state_ttl = await config_service.get_state_ttl_seconds(db)
        await drain_poll_queue(
            due,
            concurrency=self.settings.poller_poll_concurrency,
            check=lambda jid: self.check_with_sem(jid, states[jid]),
            process=lambda statuses: self.process_chunk(statuses, max_requeues, state_ttl),
            stop=self.stop,
        )

    async def run(self) -> None:
        """Poll, harvest and wait, until asked to stop."""
        logger.info("Poller worker started")
        try:
            while not self.stop.is_set():
                try:
                    wait_timeout = await self.run_pass()
                except Exception:
                    logger.exception("Poller worker failed")
                    wait_timeout = self.settings.poller_tick_seconds
                await self._flush_stages()
                await self._wait(wait_timeout)
        finally:
            await self._finish_polls()

    async def run_pass(self) -> float:
        """Start the due status checks (in the background) and harvest what's ready;
        how long to wait before the next pass."""
        self._collect_polls()
        pending_deadlines = await self._start_polls() if self.poll_task is None else []
        await self._harvest_due()
        return await self._wait_timeout(pending_deadlines)

    def _collect_polls(self) -> None:
        """Forget a finished background poll batch, logging its failure."""
        if self.poll_task is None or not self.poll_task.done():
            return

        try:
            self.poll_task.result()
        except Exception:
            logger.exception("Background poll batch failed")
        self.poll_task = None

    async def _start_polls(self) -> list[float]:
        """Start checking every in-flight job whose next poll is due; the deadlines of
        the ones that aren't due yet."""
        states = await self._inflight_states()
        now = time.time()
        due = [job_id for job_id, state in states.items() if (state.next_poll_at or 0.0) <= now]
        due_set = set(due)
        pending_deadlines = [
            state.next_poll_at or 0.0 for job_id, state in states.items() if job_id not in due_set
        ]
        if due:
            logger.info("Checking %d inflight jobs with bounded polling", len(due))
            self.poll_task = asyncio.create_task(self.drain_due(due, states))
        return pending_deadlines

    async def _inflight_states(self) -> dict[str, JobState]:
        with self.timer.stage("inflight_scan"):
            job_ids = await get_inflight_ids(self.r)
            if not job_ids:
                return {}
            states = await get_jobs(self.r, list(job_ids))  # one pipeline, not one per job
        return {job_id: state for job_id, state in states.items() if state is not None}

    async def _harvest_due(self) -> None:
        """Harvest from the durable queue: the jobs just released, retries whose backoff
        elapsed, and leftovers from a poller restart. This runs even when nothing is in
        flight, since a delayed retry can be due then."""
        harvest_limit = max(
            self.settings.poller_harvest_concurrency * 2, self.released_since_harvest
        )
        self.released_since_harvest = 0
        harvest_ids = await due_harvest_ids(self.r, harvest_limit)
        if not harvest_ids:
            return

        harvest_states = await get_jobs(self.r, harvest_ids)
        missing = [job_id for job_id, state in harvest_states.items() if state is None]
        if missing:
            logger.warning("Dropping %d harvest entr(ies) with expired job state", len(missing))
            await forget_harvests(self.r, missing)
        results = await asyncio.gather(
            *[
                self.harvest_with_sem(job_id, state, None)
                for job_id, state in harvest_states.items()
                if state is not None
            ],
            return_exceptions=True,
        )
        for harvest_result in results:
            if isinstance(harvest_result, Exception):
                logger.error("Harvest failed: %s", harvest_result)

    async def _wait_timeout(self, pending_deadlines: list[float]) -> float:
        """Wake at the soonest scheduled poll or harvest retry, capped by the idle
        ceiling (poller_tick_seconds). A due harvest (score in the past) means don't
        block at all; a dispatch (signal_poll from set_running) wakes the wait sooner."""
        wait_timeout = next_poll_timeout(
            pending_deadlines, time.time(), self.settings.poller_tick_seconds
        )
        next_harvest = await next_harvest_at(self.r)
        if next_harvest is None:
            return wait_timeout
        return min(wait_timeout, max(0.0, next_harvest - time.time()))

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
                self.http, self.settings, stage_metric_rows("poller", totals)
            )
        except Exception as exc:
            logger.warning("Stage metrics dropped: %s", exc)

    async def _wait(self, timeout: float) -> None:
        try:
            await wait_for_poll_signal(self.r, timeout)
        except Exception:  # a Redis hiccup must not end the worker
            logger.exception("Waiting for the poll signal failed")
            await asyncio.sleep(self.settings.poller_tick_seconds)

    async def _finish_polls(self) -> None:
        """Let the background poll batch finish on a stop; cancel it when this task is
        itself being cancelled."""
        if self.poll_task is None:
            return

        current_task = asyncio.current_task()
        if current_task is not None and current_task.cancelling():
            self.poll_task.cancel()
        await asyncio.gather(self.poll_task, return_exceptions=True)


def _compress(data: bytes, level: int) -> bytes:
    """Zstd-compress one output; a compressor per call, since this runs in a worker
    thread and a ``ZstdCompressor`` can't be shared between threads."""
    return zstandard.ZstdCompressor(level=level).compress(data)


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # httpx emits one INFO line per request; at poll volume it floods the log and
    # rotates real errors out of `kubectl logs`.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    db_engine = make_engine(settings)
    setup_telemetry(settings, "taas-poller", db_engine=db_engine)
    r = make_redis(settings)
    http = httpx.AsyncClient(timeout=60.0)
    emitter = get_emitter(settings)
    await emitter.start()
    worker = PollerWorker(
        settings,
        r,
        make_session_factory(db_engine),
        http,
        get_results_client(settings),
        get_results_public_client(settings),
        emitter,
        install_stop(),
    )
    try:
        await worker.run()
    finally:
        await emitter.aclose()
        await http.aclose()
        await r.aclose()
        await db_engine.dispose()
