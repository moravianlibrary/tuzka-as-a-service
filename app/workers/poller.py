"""Poller worker: polls engines for in-flight job results, stores output, and finalizes jobs."""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import zstandard
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.clock import utcnow
from app.config import Settings
from app.engines import make_adapter
from app.engines.base import EngineAdapter, EngineStatus, OutputUnavailable
from app.engines.outputs import ext_for
from app.models.backend import Backend
from app.models.db import make_engine
from app.models.job import Job, JobResult
from app.services import batch_store, live_metrics, perf
from app.services import config as config_service
from app.services.alto_stats import parse_alto
from app.services.auth import decrypt_backend_key
from app.services.event_emitter import get_emitter
from app.services.events import (
    EventName,
    JobEvent,
    attempt_failure,
    durations,
    extra_metrics,
)
from app.services.job_attempts import append_attempt
from app.services.redis_jobs import (
    batch_event,
    get_inflight_ids,
    get_job,
    job_domain,
    job_event_payload,
    job_outputs,
    publish_event,
    record_page_seconds,
    release_and_requeue,
    set_done,
    set_failed,
    wait_for_poll_signal,
)
from app.services.redis_pool import make_redis
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
from app.workers.shutdown import install_stop
from app.workers.stages import StageTimer, stage_metric_rows

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


async def fetch_outputs(
    adapter: EngineAdapter, engine_job_id: str, outputs: frozenset[str]
) -> dict[str, bytes]:
    """Fetch every requested output concurrently, before anything is stored. The engine
    serves each format independently, so fetching them in turn only adds their latencies
    (2 outputs at about half of harvest wall time at 10 backends). An output the engine
    can't produce raises ``OutputUnavailable`` and nothing is kept: a job delivers all it
    was asked for or fails (no partial results)."""
    formats = sorted(outputs)
    fetched = await asyncio.gather(*(adapter.fetch(engine_job_id, output) for output in formats))
    return {output: result.content for output, result in zip(formats, fetched, strict=True)}


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


def engine_stamps(times: dict[str, Any]) -> dict[str, datetime]:
    """The engine's own created/started/finished (engine clock) as job columns, for
    the ones it reported. created_at goes to engine_received_at, so the engine-queue
    span (engine_received -> started) stays on one clock; the taas-clock dispatched_at
    is never overwritten. Shared by done and failed jobs so both record the run."""
    stamps: dict[str, datetime] = {}
    for key, column in (
        ("created_at", "engine_received_at"),
        ("started_at", "started_at"),
        ("finished_at", "finished_at"),
    ):
        value = _parse_engine_dt(times.get(key))
        if value is not None:
            stamps[column] = value
    return stamps


def _status_times(status: EngineStatus) -> dict[str, Any]:
    """What harvest reads from a status: the engine's metrics plus its timestamps
    (engine clock), which feed the job's engine_received/started/finished columns."""
    return {
        **status.metrics,
        "created_at": status.created_at,
        "started_at": status.started_at,
        "finished_at": status.finished_at,
    }


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
    timer = StageTimer()
    stages_flushed_at = time.monotonic()
    db_engine = make_engine(settings)
    setup_telemetry("taas-poller", db_engine=db_engine)
    session_factory = async_sessionmaker(db_engine, expire_on_commit=False)
    r = make_redis(settings)
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
                job_id, status_resp.state, meta, _status_times(status_resp), False, None, None
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
            _status_times(status_resp),
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

            with timer.stage("fetch_outputs"):
                fetched = await fetch_outputs(adapter, engine_job_id, outputs)
            for output, content in fetched.items():
                obj_key = f"{username}/{job_id}.{ext_for(output)}.zst"
                results_to_store.append((output, obj_key, content))
                if output == "alto":
                    alto_bytes_for_analytics = content
                elif output == "txt":
                    txt_bytes_for_analytics = content

            # Line count comes straight from the engine's status — it reports n_lines for
            # every job (computed during OCR), so lines are populated regardless of fmt.
            alto_lines = times.get("n_lines") if isinstance(times.get("n_lines"), int) else None
            # Blocks are ALTO-only, so parse those (present for multi/alto) before compression.
            alto_blocks = alto_chars = None
            if alto_bytes_for_analytics:
                with timer.stage("parse_alto"):
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
                result_rows: list[dict[str, Any]] = []
                for result_fmt, obj_path, raw_bytes in results_to_store:
                    with timer.stage("compress"):
                        compressed = cctx.compress(raw_bytes)
                    with timer.stage("store_put"):
                        await put_object(
                            results_client,
                            settings.minio_results_bucket,
                            obj_path,
                            compressed,
                            "application/octet-stream",
                        )
                    logger.info(f"Stored {obj_path} ({len(compressed)} bytes)")

                    with timer.stage("presign"):
                        presigned_url = await presign_get(
                            results_public_client,
                            settings.minio_results_bucket,
                            obj_path,
                            presigned_ttl,
                        )

                    result_rows.append(
                        {
                            "job_id": job_id,
                            "fmt": result_fmt,
                            "presigned_url": presigned_url,
                            "presigned_until": utcnow() + timedelta(minutes=presigned_ttl),
                            "size_bytes": len(compressed),  # what the storage cap counts
                            "evicted_at": None,
                        }
                    )
                    event_data[f"{result_fmt}_url"] = presigned_url  # alto_url, geojson_url, …

                stored_at = utcnow()
                done_values: dict[str, str | datetime] = {
                    "status": "done",
                    "stored_at": stored_at,
                }
                stamps = engine_stamps(times)
                done_values.update(stamps)
                engine_created = stamps.get("engine_received_at")
                engine_started = stamps.get("started_at")
                engine_finished = stamps.get("finished_at")
                finished_at: datetime = engine_finished or utcnow()
                done_values["finished_at"] = finished_at
                # Only the first harvest marks it done; a second one (another poller
                # got there first) must not report the job, its events or its timing again.
                with timer.stage("db_write"):
                    if result_rows:
                        # One statement for every output: with the job update below, a
                        # two-output job costs two round trips instead of three. An
                        # upsert, because a re-harvest rewrote the same objects.
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
                        .where(Job.id == job_id, Job.status != "done")
                        .values(**done_values)
                    )
                    first_harvest = getattr(claimed, "rowcount", 1) == 1
                    await batch_store.finish_if_done(db, meta.get("batch_id"))
                    await db.commit()

            with timer.stage("set_done"):
                batch = await set_done(r, job_id, state_ttl)  # idempotent
            if not first_harvest:
                logger.info("Job %s was already harvested; not reporting it again", job_id)
                if batch is not None:  # the first harvest died before its set_done
                    await publish_event(r, username, batch_event(batch))
                return
            # Engine-clock run time per megapixel scores this backend for dispatch.
            if backend_id is not None and engine_started and engine_finished:
                seconds = (engine_finished - engine_started).total_seconds()
                if seconds > 0:
                    await perf.record_service(
                        r, backend_id, job_domain(meta), seconds, float(meta.get("mp") or 1.0)
                    )
            # Engine-clock run time also feeds /load's seconds-per-page estimate.
            if engine_started is not None and engine_finished is not None:
                seconds = (engine_finished - engine_started).total_seconds()
                if seconds > 0:
                    await record_page_seconds(r, job_domain(meta), seconds)
            with timer.stage("events"):
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
                        extra=extra_metrics(times),
                        **durations(
                            job_rec.submitted_at if job_rec else None,
                            job_rec.dispatched_at if job_rec else None,
                            engine_created,
                            engine_started,
                            finished_at,
                            stored_at,
                        ),
                    ),
                )
                await publish_event(r, username, event_data)
                if batch is not None:
                    await publish_event(r, username, batch_event(batch))
            logger.info(f"Published done event for {username}")

        except OutputUnavailable as e:
            # The engine can't produce an output this job asked for (TuzkaM: georeferencing
            # failed). Retrying gives the same answer, so the job fails for good.
            logger.warning("Job %s: %s", job_id, e)
            attempt = attempt_entry(
                "harvest", backend_id, 409, str(e), retry=False, at=utcnow().isoformat()
            )
            await mark_failed(job_id, meta, str(e), attempt=attempt, times=times)
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
            await mark_failed(job_id, meta, str(e), times=times)

    async def mark_failed(
        job_id: str,
        meta: dict[str, str],
        error: str,
        attempt: dict[str, Any] | None = None,
        times: dict[str, Any] | None = None,
    ) -> None:
        """Fail the job for good. ``times`` is the engine's last status, when it ran the
        job: its stamps are kept, so a job that failed after minutes of work shows them."""
        username = meta.get("username", "")
        external_id = meta.get("external_id") or None
        failed_at = utcnow()
        async with session_factory() as db:
            state_ttl = await config_service.get_state_ttl_seconds(db)
            # A job another harvest already finished stays done: a late failure (say a
            # second harvest that lost the race) mustn't overwrite its results.
            done = await db.scalar(select(Job.status).where(Job.id == job_id)) == "done"
            if done:
                logger.warning("Job %s is already done; not failing it: %s", job_id, error)
                await set_done(r, job_id, state_ttl)
                return
            batch = await set_failed(r, job_id, error, state_ttl)
            if (
                attempt
                and attempt.get("backend_id") is not None
                and perf.counts_against_backend(attempt.get("http_status"))
            ):
                await perf.record_failure(r, int(attempt["backend_id"]), job_domain(meta))
            values: dict[str, Any] = {
                "status": "failed",
                "error": error,
                "finished_at": failed_at,
                **engine_stamps(times or {}),
            }
            finished_at = values["finished_at"]
            if attempt is not None:
                values.update(append_attempt(attempt))
            await db.execute(
                update(Job).where(Job.id == job_id, Job.status != "done").values(**values)
            )

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
                    finished_at,
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
    # Bounds a pass's status checks: one per due job, so hundreds at once otherwise,
    # each holding an engine and a Redis connection.
    poll_sem = asyncio.Semaphore(settings.poller_poll_concurrency)

    async def check_with_sem(job_id: str, meta: dict[str, str]) -> PollOutcome:
        with job_span("poll", job_id, meta, backend_id=meta.get("backend_id")), timer.stage("poll"):
            with timer.stage("poll_wait"):
                await poll_sem.acquire()
            try:
                return await check_one(job_id, meta)
            finally:
                poll_sem.release()

    async def harvest_with_sem(job_id: str, meta: dict[str, str], times: dict[str, Any]) -> None:
        with timer.stage("harvest_wait"):
            await sem.acquire()
        try:
            with job_span("harvest", job_id, meta, backend_id=meta.get("backend_id")):
                await harvest(job_id, meta, times)
        finally:
            sem.release()

    stop = install_stop()
    logger.info("Poller worker started")
    try:
        while not stop.is_set():
            # How long to wait before the next pass. Recomputed each iteration from the
            # nearest scheduled next_poll_at, so we wake exactly when a running job is due
            # rather than on a coarse grid; capped by poller_tick_seconds as an idle/self-
            # heal ceiling. A dispatch (signal_poll from set_running) wakes us sooner still.
            wait_timeout = settings.poller_tick_seconds
            try:
                with timer.stage("inflight_scan"):
                    job_ids = await get_inflight_ids(r)
                    metas_by_id: dict[str, dict[str, str]] = {}
                    if job_ids:
                        metas = await asyncio.gather(*(get_job(r, jid) for jid in job_ids))
                        metas_by_id = {jid: m for jid, m in zip(job_ids, metas, strict=True) if m}
                now = time.time()

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
                        *[check_with_sem(jid, metas_by_id[jid]) for jid in due]
                    )

                    done_jobs: list[tuple[str, dict[str, str], dict[str, Any]]] = []
                    failed_jobs: list[
                        tuple[str, dict[str, str], str, dict[str, Any], dict[str, Any]]
                    ] = []
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
                        with timer.stage("classify"):
                            action = classify_poll_result(
                                outcome.status, outcome.retryable, requeues, max_requeues
                            )
                        if action == "harvest":
                            done_jobs.append((outcome.job_id, meta, outcome.times))
                        elif action == "fail":
                            err = poll_failure_message(outcome, max_requeues)
                            attempt = poll_attempt(outcome, False, utcnow().isoformat())
                            failed_jobs.append((outcome.job_id, meta, err, attempt, outcome.times))
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
                    for job_id, meta, error, attempt, times in failed_jobs:
                        with (
                            job_span("fail", job_id, meta, backend_id=meta.get("backend_id")),
                            timer.stage("fail"),
                        ):
                            await mark_failed(job_id, meta, error, attempt=attempt, times=times)

                    # Re-queue retryable failures (unreachable pod, engine 5xx, lost job) so
                    # another backend picks them up after a jittered backoff. Redis is
                    # updated first (release slot + schedule the retry + bump the requeue
                    # counter the budget reads); the DB mirrors it, with the attempt
                    # recorded, in one batched session.
                    if requeue_jobs:
                        for job_id, meta, delay, avoid_id, _attempt in requeue_jobs:
                            with timer.stage("requeue"):
                                await release_and_requeue(
                                    r,
                                    job_id,
                                    float(meta.get("submitted_at", time.time())),
                                    state_ttl,
                                    delay_s=delay,
                                    avoid_backend_id=avoid_id,
                                )
                                await r.hincrby(f"job:{job_id}", "requeues", 1)
                                if avoid_id is not None:  # retryable: the backend's fault
                                    await perf.record_failure(r, avoid_id, job_domain(meta))
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

            if time.monotonic() - stages_flushed_at >= 60:
                stages_flushed_at = time.monotonic()
                p95 = timer.p95()
                totals = timer.flush()
                for stage, (count, total, maximum) in sorted(totals.items()):
                    logger.info(
                        "stage=%s n=%d total_s=%.6f max_s=%.6f p95_s=%.6f",
                        stage,
                        count,
                        total,
                        maximum,
                        p95[stage],
                    )
                if settings.clickhouse_url and totals:
                    try:
                        await live_metrics.insert_rows(
                            http, settings, stage_metric_rows("poller", totals)
                        )
                    except Exception as exc:
                        logger.warning("Stage metrics dropped: %s", exc)

            try:
                await wait_for_poll_signal(r, wait_timeout)
            except Exception:  # a Redis hiccup must not end the worker
                logger.exception("Waiting for the poll signal failed")
                await asyncio.sleep(settings.poller_tick_seconds)
    finally:
        await emitter.aclose()
        await http.aclose()
        await r.aclose()
        await db_engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
