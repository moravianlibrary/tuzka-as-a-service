"""Poller worker: polls engines for in-flight job results, stores output, and finalizes jobs."""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import redis.asyncio as aioredis
import zstandard
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clock import utcnow
from app.config import Settings
from app.engines import make_adapter
from app.engines.base import EngineAdapter
from app.engines.outputs import ext_for
from app.models.backend import Backend
from app.models.db import make_engine
from app.models.job import Job, JobResult
from app.services import batch_store
from app.services import config as config_service
from app.services.alto_stats import parse_alto
from app.services.auth import decrypt_backend_key
from app.services.event_emitter import get_emitter
from app.services.events import EventName, JobEvent, attempt_failure, durations
from app.services.job_attempts import append_attempt
from app.services.redis_jobs import (
    get_inflight_ids,
    get_job,
    job_domain,
    batch_event,
    job_event_payload,
    job_outputs,
    publish_event,
    release_and_requeue,
    set_done,
    set_failed,
    wait_for_poll_signal,
)
from app.services.retry_policy import (
    Stage,
    attempt_entry,
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

logger = logging.getLogger("poller-worker")


def is_transient_harvest_error(exc: Exception) -> bool:
    """True if a harvest failure is infrastructure (DB/storage), not a bad result.

    The engine has already produced the OCR output by the time we harvest, so a dropped
    DB connection or a transient storage/transport error must NOT permanently fail the
    job: we leave it inflight and let the next poll re-harvest it (the reaper is the
    backstop if it truly stays stuck). A pooled connection closed under us surfaces as a
    DBAPIError with ``connection_invalidated`` set (e.g. asyncpg's
    ConnectionDoesNotExistError). Pure (no I/O) so it is unit-testable.
    """
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    return isinstance(exc, httpx.TransportError)


def _parse_engine_dt(value: str | None) -> datetime | None:
    """Parse an engine ISO-8601 timestamp into a naive UTC datetime (the rest of
    taas stores naive UTC). Returns None for missing/invalid input. A naive engine
    timestamp (no offset) is assumed to already be UTC and stored as-is."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    return dt


@dataclass(frozen=True, slots=True)
class PollOutcome:
    """What one status check of an in-flight job found, plus how to treat a failure."""

    job_id: str
    status: str  # "done", "running", "failed", "error", "unreachable", "unknown"
    meta: dict[str, str]
    times: dict[str, Any]
    retryable: bool  # a failure here may succeed on another backend
    http_status: int | None
    error: str | None
    # "engine" when the engine reported the failure itself, "poll" when the status
    # check failed (transport error, HTTP error from the status endpoint).
    stage: Stage = "poll"


def classify_poll_result(status: str, retryable: bool, requeues: int, max_requeues: int) -> str:
    """Decide what to do with a polled job. Pure (no I/O) so it's unit-testable.

    Failures ('failed'/'error' from the engine or the status check, 'unreachable' when
    the pod couldn't be reached) requeue while they're retryable and the budget lasts,
    and fail otherwise. Whether a failure is retryable is the retry policy's call.
    """
    if status == "done":
        return "harvest"
    if status in ("failed", "error", "unreachable"):
        return "requeue" if retryable and requeues < max_requeues else "fail"
    return "running"


def poll_failure_message(outcome: PollOutcome, max_requeues: int) -> str:
    """The job's ``error`` text when a poll outcome fails it. Pure."""
    if outcome.status == "unreachable":
        return f"engine unreachable, exceeded {max_requeues} requeue attempts"
    return outcome.error or outcome.meta.get("error") or "Engine error"


def poll_attempt(outcome: PollOutcome, retry: bool, at: str) -> dict[str, Any]:
    """The ``jobs.attempts`` entry for a failed poll outcome. Pure."""
    backend_id = outcome.meta.get("backend_id")
    return attempt_entry(
        stage=outcome.stage,
        backend_id=int(backend_id) if backend_id else None,
        http_status=outcome.http_status,
        error=outcome.error or outcome.status,
        retry=retry,
        at=at,
    )


def next_poll_timeout(deadlines: list[float], now: float, cap: float) -> float:
    """How long to wait before the next poll pass. Pure (no I/O) so it's unit-testable.

    Wakes at the soonest scheduled ``next_poll_at`` so a running job is polled right when
    its backoff deadline arrives (not on a coarse grid), clamped to ``[0, cap]``: a past
    deadline means poll now (0), and ``cap`` is the idle/self-heal ceiling. With no pending
    deadlines — nothing in flight — wait the full ``cap``; a dispatch signal wakes sooner.
    """
    if not deadlines:
        return cap
    return min(cap, max(0.0, min(deadlines) - now))


async def _probe_failure_status(adapter: EngineAdapter, engine_job_id: str) -> int | None:
    """Probe a failed job's error status by fetching the result endpoint.

    Engines that predate ``error_status`` in the status response still raise the
    job's error status as the result endpoint's HTTP status (422 for bad input, 500
    for a crash), which gives the 4xx/5xx split retry classification needs. Returns
    ``None`` when that tells us nothing (non-error answer, or engine unreachable).
    """
    try:
        await adapter.fetch(engine_job_id, "alto")
    except httpx.HTTPStatusError as exc:
        return exc.response.status_code
    except Exception as exc:
        logger.warning("probe_failure_status failed for %s: %s", engine_job_id, exc)
    return None


async def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # httpx emits one INFO line per request; at poll volume it floods the log and
    # rotates real errors out of `kubectl logs`.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = Settings()
    db_engine = make_engine(settings)
    setup_telemetry("taas-poller", db_engine=db_engine)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    r = aioredis.from_url(settings.redis_url, decode_responses=False)
    http = httpx.AsyncClient(timeout=60.0)
    results_client = get_results_client(settings)
    results_public_client = get_results_public_client(settings)
    cctx = zstandard.ZstdCompressor(level=settings.zstd_compression_level)
    emitter = get_emitter(settings)
    await emitter.start()

    # backend id -> (adapter id, decrypted key), read from the DB once per backend.
    backend_info: dict[int, tuple[str, str | None]] = {}
    # (backend id, url) -> adapter. The URL comes from the job hash, i.e. where the job
    # was actually dispatched, so a backend re-registered at a new URL gets a new adapter.
    adapters: dict[tuple[int | None, str], EngineAdapter] = {}

    async def adapter_for(meta: dict[str, str]) -> EngineAdapter:
        backend_id = int(meta["backend_id"]) if meta.get("backend_id") else None
        url = meta["backend_url"]
        if backend_id is not None and backend_id not in backend_info:
            async with session_factory() as db:
                backend = await db.get(Backend, backend_id)
            adapter_id, key = "tuzkaocr", None
            if backend:
                adapter_id = backend.adapter
                if backend.api_key_enc:
                    key = decrypt_backend_key(backend.api_key_enc, settings.key_encryption_secret)
            backend_info[backend_id] = (adapter_id, key)
        adapter_id, key = backend_info[backend_id] if backend_id is not None else ("tuzkaocr", None)
        cache_key = (backend_id, url)
        if cache_key not in adapters:
            adapters[cache_key] = make_adapter(adapter_id, url, key, http)
        return adapters[cache_key]

    def job_event(
        name: EventName, job_id: str, meta: dict[str, str], job_rec: Job | None, **fields: Any
    ) -> JobEvent:
        """An event for a job the poller is finishing, from its hash and DB row."""
        backend_id = int(meta["backend_id"]) if meta.get("backend_id") else None
        adapter = backend_info[backend_id][0] if backend_id in backend_info else ""
        return JobEvent(
            ts=utcnow(),
            event=name,
            job_id=job_id,
            username=meta.get("username", ""),
            domain=job_domain(meta),
            outputs=sorted(job_outputs(meta)),
            backend_id=backend_id,
            adapter=adapter,
            engine_version=(job_rec.engine_version if job_rec else None) or "",
            file_size_bytes=job_rec.file_size_bytes if job_rec else None,
            **fields,
        )

    async def check_one(job_id: str, meta: dict[str, str]) -> PollOutcome:
        try:
            adapter = await adapter_for(meta)
            engine_job_id = meta["engine_job_id"]
            # In the job's trace, so its polls show up there instead of as stray traces.
            with job_span("poll", job_id, meta, backend_id=meta.get("backend_id")):
                status_resp = await adapter.status(engine_job_id)
        except httpx.TransportError as e:
            logger.warning(f"Engine unreachable for job {job_id}: {e}")
            return PollOutcome(job_id, "unreachable", meta, {}, True, None, describe_exception(e))
        except Exception as e:
            logger.error(f"Failed to check status for job {job_id}: {e}")
            return PollOutcome(
                job_id,
                "error",
                meta,
                {},
                is_retryable_poll_error(e),
                http_status_of(e),
                describe_exception(e),
            )

        if status_resp.state not in ("failed", "error"):
            return PollOutcome(
                job_id, status_resp.state, meta, status_resp.metrics, False, None, None
            )
        # Engine-reported failure: error_status decides retry vs fail. Engines that
        # predate the field still expose it as the result endpoint's HTTP status (D3).
        error_status = status_resp.error_status
        if error_status is None:
            error_status = await _probe_failure_status(adapter, engine_job_id)
        return PollOutcome(
            job_id,
            status_resp.state,
            meta,
            status_resp.metrics,
            is_retryable_engine_failure(error_status),
            error_status,
            status_resp.error,
            stage="engine",
        )

    async def harvest(job_id: str, meta: dict[str, str], times: dict[str, Any]) -> None:
        username = meta["username"]
        external_id = meta.get("external_id") or None
        engine_job_id = meta["engine_job_id"]
        backend_id = int(meta["backend_id"]) if meta.get("backend_id") else None

        adapter = await adapter_for(meta)
        outputs = job_outputs(meta)
        try:
            results_to_store: list[tuple[str, str, bytes]] = []
            alto_bytes_for_analytics: bytes | None = None
            txt_bytes_for_analytics: bytes | None = None

            for output in sorted(outputs):
                res = await adapter.fetch(engine_job_id, output)
                obj_key = f"{username}/{job_id}.{ext_for(output)}.zst"
                results_to_store.append((output, obj_key, res.content))
                if output == "alto":
                    alto_bytes_for_analytics = res.content
                elif output == "txt":
                    txt_bytes_for_analytics = res.content

            # Line count comes straight from the engine's status — it reports n_lines for
            # every job (computed during OCR), so lines are populated regardless of fmt.
            alto_lines = times.get("n_lines") if isinstance(times.get("n_lines"), int) else None
            # Blocks are ALTO-only, so parse those (present for multi/alto) before compression.
            alto_blocks = alto_chars = None
            if alto_bytes_for_analytics:
                alto_blocks, alto_chars = parse_alto(alto_bytes_for_analytics)
            # Prefer counting characters from the txt output (present for txt/multi jobs) so
            # the metric is populated regardless of fmt; count non-whitespace to match ALTO's
            # char semantics (sum of String CONTENT). ALTO's count stays for alto-only jobs.
            if txt_bytes_for_analytics is not None:
                text = txt_bytes_for_analytics.decode("utf-8", "replace")
                alto_chars = sum(1 for c in text if not c.isspace())

            mean_conf: float | None = None
            if isinstance(times.get("mean_conf"), (int, float)):
                mean_conf = float(times["mean_conf"])

            event_data = job_event_payload(
                job_id, external_id, "done", batch_id=meta.get("batch_id")
            )

            async with session_factory() as db:
                # Read the raw job to pick up submitted_at, engine_version, device, file_size.
                job_row = await db.execute(
                    select(Job, Backend.device)
                    .outerjoin(Backend, Job.backend_id == Backend.id)
                    .where(Job.id == job_id)
                )
                job_rec, backend_device = job_row.first() or (None, None)

                presigned_ttl = await config_service.get_presigned_ttl_minutes(db)
                state_ttl = await config_service.get_state_ttl_seconds(db)
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
                        results_public_client,
                        settings.minio_results_bucket,
                        obj_path,
                        presigned_ttl,
                    )

                    jr = JobResult(
                        job_id=job_id,
                        fmt=result_fmt,
                        presigned_url=presigned_url,
                        presigned_until=utcnow() + timedelta(minutes=presigned_ttl),
                        size_bytes=len(compressed),  # what the results storage cap counts
                    )
                    db.add(jr)

                    url_key = "alto_url" if result_fmt == "alto" else "txt_url"
                    event_data[url_key] = presigned_url

                stored_at = utcnow()
                done_values: dict[str, str | datetime] = {
                    "status": "done",
                    "stored_at": stored_at,
                }
                # Adopt the engine's own created/started/finished (engine clock) into the
                # engine-side stamps. We keep the taas-clock dispatched_at as set by the
                # submit worker (do NOT overwrite it): that preserves a single-clock
                # taas-queue span (submitted -> dispatched). created_at lands in its own
                # engine_received_at column so the engine-queue span (engine_received ->
                # started) is also single-clock and never goes negative on sub-second waits.
                engine_created = _parse_engine_dt(times.get("created_at"))
                engine_started = _parse_engine_dt(times.get("started_at"))
                engine_finished = _parse_engine_dt(times.get("finished_at"))
                if engine_created is not None:
                    done_values["engine_received_at"] = engine_created
                if engine_started is not None:
                    done_values["started_at"] = engine_started
                finished_at: datetime = engine_finished or utcnow()
                done_values["finished_at"] = finished_at
                await db.execute(update(Job).where(Job.id == job_id).values(**done_values))
                await batch_store.finish_if_done(db, meta.get("batch_id"))

                await db.commit()

            batch = await set_done(r, job_id, state_ttl)
            emitter.emit(
                job_event(
                    "done",
                    job_id,
                    meta,
                    job_rec,
                    device=backend_device or "",
                    lines=alto_lines,
                    blocks=alto_blocks,
                    chars=alto_chars,
                    mean_conf=mean_conf,
                    **durations(
                        job_rec.submitted_at if job_rec else None,
                        job_rec.dispatched_at if job_rec else None,
                        engine_created,
                        engine_started,
                        finished_at,
                        stored_at,
                    ),
                )
            )
            await publish_event(r, username, event_data)
            if batch is not None:
                await publish_event(r, username, batch_event(batch))
            logger.info(f"Published done event for {username}")

        except Exception as e:
            # The engine already finished this job; a transient DB/storage error here is
            # not the job's fault. Leave it inflight so the next poll re-harvests it (the
            # reaper fails it eventually if it truly stays stuck) rather than stamping a
            # raw infrastructure traceback as a permanent user-visible failure.
            if is_transient_harvest_error(e):
                logger.warning(
                    "Transient error harvesting job %s; leaving inflight to retry: %s",
                    job_id,
                    e,
                )
                return
            logger.exception("Failed to harvest job %s", job_id)
            await mark_failed(job_id, meta, str(e))

    async def mark_failed(
        job_id: str, meta: dict[str, str], error: str, attempt: dict[str, Any] | None = None
    ) -> None:
        username = meta.get("username", "")
        external_id = meta.get("external_id") or None
        failed_at = utcnow()
        async with session_factory() as db:
            state_ttl = await config_service.get_state_ttl_seconds(db)
            batch = await set_failed(r, job_id, error, state_ttl)
            values: dict[str, Any] = {
                "status": "failed",
                "error": error,
                "finished_at": failed_at,
            }
            if attempt is not None:
                values.update(append_attempt(attempt))
            await db.execute(update(Job).where(Job.id == job_id).values(**values))

            # The job as it ended, for its failed event.
            job_row = await db.execute(
                select(Job, Backend.device)
                .outerjoin(Backend, Job.backend_id == Backend.id)
                .where(Job.id == job_id)
            )
            job_rec, backend_device = job_row.first() or (None, None)
            await batch_store.finish_if_done(db, meta.get("batch_id"))

            await db.commit()
        emitter.emit(
            job_event(
                "failed",
                job_id,
                meta,
                job_rec,
                device=backend_device or "",
                **attempt_failure(attempt),
                **durations(
                    job_rec.submitted_at if job_rec else None,
                    job_rec.dispatched_at if job_rec else None,
                    job_rec.engine_received_at if job_rec else None,
                    job_rec.started_at if job_rec else None,
                    failed_at,
                    None,
                ),
            )
        )
        await publish_event(
            r,
            username,
            job_event_payload(
                job_id, external_id, "failed", batch_id=meta.get("batch_id"), error=error
            ),
        )
        if batch is not None:
            await publish_event(r, username, batch_event(batch))

    sem = asyncio.Semaphore(settings.poller_harvest_concurrency)

    async def harvest_with_sem(job_id: str, meta: dict[str, str], times: dict[str, Any]) -> None:
        async with sem:
            with job_span("harvest", job_id, meta, backend_id=meta.get("backend_id")):
                await harvest(job_id, meta, times)

    logger.info("Poller worker started")
    try:
        while True:
            # How long to wait before the next pass. Recomputed each iteration from the
            # nearest scheduled next_poll_at, so we wake exactly when a running job is due
            # rather than on a coarse grid; capped by poller_tick_seconds as an idle/self-
            # heal ceiling. A dispatch (signal_poll from set_running) wakes us sooner still.
            wait_timeout = settings.poller_tick_seconds
            try:
                job_ids = await get_inflight_ids(r)
                now = time.time()
                metas_by_id: dict[str, dict[str, str]] = {}
                if job_ids:
                    metas = await asyncio.gather(*(get_job(r, jid) for jid in job_ids))
                    metas_by_id = {jid: m for jid, m in zip(job_ids, metas, strict=True) if m}

                due = [
                    jid for jid, m in metas_by_id.items() if float(m.get("next_poll_at", 0)) <= now
                ]
                due_set = set(due)
                # Jobs not due yet keep their scheduled time; we must wake for the soonest.
                pending_deadlines = [
                    float(m.get("next_poll_at", 0))
                    for jid, m in metas_by_id.items()
                    if jid not in due_set
                ]

                if due:
                    logger.info(f"Checking {len(due)} inflight jobs")
                    statuses = await asyncio.gather(
                        *[check_one(jid, metas_by_id[jid]) for jid in due]
                    )

                    done_jobs: list[tuple[str, dict[str, str], dict[str, Any]]] = []
                    failed_jobs: list[tuple[str, dict[str, str], str, dict[str, Any]]] = []
                    requeue_jobs: list[
                        tuple[str, dict[str, str], float, int | None, dict[str, Any]]
                    ] = []
                    running_jobs: list[tuple[str, dict[str, str]]] = []

                    async with session_factory() as db:
                        max_requeues = await config_service.get_max_requeues(db)
                        state_ttl = await config_service.get_state_ttl_seconds(db)

                    for outcome in statuses:
                        meta = outcome.meta
                        requeues = int(meta.get("requeues", 0))
                        action = classify_poll_result(
                            outcome.status, outcome.retryable, requeues, max_requeues
                        )
                        if action == "harvest":
                            done_jobs.append((outcome.job_id, meta, outcome.times))
                        elif action == "fail":
                            err = poll_failure_message(outcome, max_requeues)
                            attempt = poll_attempt(outcome, False, utcnow().isoformat())
                            failed_jobs.append((outcome.job_id, meta, err, attempt))
                        elif action == "requeue":
                            delay = backoff_seconds(requeues)
                            avoid: int | None = (
                                int(meta["backend_id"]) if meta.get("backend_id") else None
                            )
                            attempt = poll_attempt(outcome, True, utcnow().isoformat())
                            requeue_jobs.append((outcome.job_id, meta, delay, avoid, attempt))
                        else:
                            running_jobs.append((outcome.job_id, meta))

                    # Update next_poll_at with backoff for running jobs; remember each new
                    # deadline so the wait below fires right when the soonest job is due.
                    for job_id, meta in running_jobs:
                        current_backoff = float(meta.get("next_poll_at", time.time())) - float(
                            meta.get("last_poll", time.time())
                        )
                        next_backoff = min(
                            max(current_backoff * 2, settings.poll_backoff_initial),
                            settings.poll_backoff_max,
                        )
                        new_deadline = time.time() + next_backoff
                        await r.hset(
                            f"job:{job_id}",
                            mapping={
                                "next_poll_at": str(new_deadline),
                                "last_poll": str(time.time()),
                            },
                        )
                        pending_deadlines.append(new_deadline)

                    # Harvest done jobs
                    if done_jobs:
                        results = await asyncio.gather(
                            *[harvest_with_sem(jid, meta, times) for jid, meta, times in done_jobs],
                            return_exceptions=True,
                        )
                        for result in results:
                            if isinstance(result, Exception):
                                logger.error("Harvest failed: %s", result)

                    # Mark failed jobs
                    for job_id, meta, error, attempt in failed_jobs:
                        with job_span("fail", job_id, meta, backend_id=meta.get("backend_id")):
                            await mark_failed(job_id, meta, error, attempt=attempt)

                    # Re-queue retryable failures (unreachable pod, engine 5xx, lost job) so
                    # another backend picks them up after a jittered backoff. Redis is
                    # updated first (release slot + schedule the retry + bump the requeue
                    # counter the budget reads); the DB mirrors it, with the attempt
                    # recorded, in one batched session.
                    if requeue_jobs:
                        for job_id, meta, delay, avoid_id, _attempt in requeue_jobs:
                            await release_and_requeue(
                                r,
                                job_id,
                                float(meta.get("submitted_at", time.time())),
                                state_ttl,
                                delay_s=delay,
                                avoid_backend_id=avoid_id,
                            )
                            await r.hincrby(f"job:{job_id}", "requeues", 1)
                            logger.info(
                                f"Re-queued job {job_id} in {delay:.1f}s (avoiding backend {avoid_id})"
                            )
                        async with session_factory() as db:
                            for job_id, _meta, _delay, _avoid, attempt in requeue_jobs:
                                await db.execute(
                                    update(Job)
                                    .where(Job.id == job_id)
                                    .values(
                                        status="queued",
                                        engine_job_id=None,
                                        backend_id=None,
                                        dispatched_at=None,
                                        requeues=Job.requeues + 1,
                                        **append_attempt(attempt),
                                    )
                                )
                            await db.commit()
                        for job_id, meta, _delay, _avoid, attempt in requeue_jobs:
                            emitter.emit(
                                job_event("retried", job_id, meta, None, **attempt_failure(attempt))
                            )

                # Wake at the soonest scheduled poll, capped by the idle ceiling.
                wait_timeout = next_poll_timeout(
                    pending_deadlines, time.time(), settings.poller_tick_seconds
                )

            except Exception:
                logger.exception("Poller worker failed")
                wait_timeout = settings.poller_tick_seconds

            await wait_for_poll_signal(r, wait_timeout)
    finally:
        await emitter.aclose()
        await http.aclose()
        await r.aclose()
        await db_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
