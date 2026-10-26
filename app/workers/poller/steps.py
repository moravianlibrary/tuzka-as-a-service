"""The poller's stateless steps: classifying a poll, engine timestamps, the bounded
poll drain. Pure where possible, so they're unit-testable without the worker."""

import asyncio
import logging
import random
from collections.abc import Awaitable, Callable, Iterable
from datetime import UTC, datetime

import httpx
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeout

from app.engines.base import EngineAdapter, EngineStatus
from app.schemas.attempt import Attempt
from app.schemas.redis import EngineRun, JobState
from app.services.retry_policy import backoff_seconds
from app.vocabulary import (
    PollAction,
    PollStatus,
)
from app.workers.poller.types import (
    FailedPoll,
    OutputStats,
    PollOutcome,
    PollPlan,
    RequeuedPoll,
)

logger = logging.getLogger("poller-worker")


def is_transient_harvest_error(exc: Exception) -> bool:
    """True if a harvest failure is infrastructure (DB/storage), not a bad result.

    The engine has already produced the OCR output by the time we harvest, so a dropped
    DB connection or a transient storage/transport error must NOT permanently fail the
    job: it stays on the harvest queue and is retried after a backoff (the reaper is the
    backstop if it truly stays stuck). A pooled connection closed under us surfaces as a
    DBAPIError with ``connection_invalidated`` set (e.g. asyncpg's
    ConnectionDoesNotExistError); a new connection that can't be opened (database down,
    refused, unresolvable) as an ``OSError``, and an exhausted pool as SQLAlchemy's
    ``TimeoutError``. Pure (no I/O) so it is unit-testable.
    """
    if isinstance(exc, DBAPIError) and exc.connection_invalidated:
        return True
    return isinstance(exc, httpx.TransportError | OSError | PoolTimeout)


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


def engine_stamps(run: EngineRun) -> dict[str, datetime]:
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
        value = _parse_engine_dt(getattr(run, key))
        if value is not None:
            stamps[column] = value
    return stamps


def engine_run(status: EngineStatus) -> EngineRun:
    """What harvest reads from a status: the engine's metrics plus its timestamps
    (engine clock), which feed the job's engine_received/started/finished columns."""
    return EngineRun(
        created_at=status.created_at,
        started_at=status.started_at,
        finished_at=status.finished_at,
        metrics=status.metrics,
    )


def classify_poll_result(
    status: PollStatus, retryable: bool, requeues: int, max_requeues: int
) -> PollAction:
    """Decide what to do with a polled job. Pure (no I/O) so it's unit-testable.

    Failures ('failed'/'error' from the engine or the status check, 'unreachable' when
    the pod couldn't be reached) requeue while they're retryable and the budget lasts,
    and fail otherwise. Whether a failure is retryable is the retry policy's call.
    """
    if status == PollStatus.DONE:
        return PollAction.HARVEST
    if status in (PollStatus.FAILED, PollStatus.ERROR, PollStatus.UNREACHABLE):
        return PollAction.REQUEUE if retryable and requeues < max_requeues else PollAction.FAIL
    return PollAction.RUNNING


def poll_failure_message(outcome: PollOutcome, max_requeues: int) -> str:
    """The job's ``error`` text when a poll outcome fails it. Pure."""
    if outcome.status == PollStatus.UNREACHABLE:
        return f"engine unreachable, exceeded {max_requeues} requeue attempts"
    return outcome.error or outcome.state.error or "Engine error"


def poll_attempt(outcome: PollOutcome, retry: bool, at: datetime) -> Attempt:
    """The ``jobs.attempts`` entry for a failed poll outcome. Pure."""
    return Attempt(
        stage=outcome.stage,
        backend_id=outcome.state.backend_id,
        http_status=outcome.http_status,
        error=outcome.error or outcome.status,
        retry=retry,
        at=at,
    )


def plan_polls(
    statuses: list[PollOutcome], max_requeues: int, now: datetime, rng: random.Random
) -> PollPlan:
    """Sort a chunk of poll outcomes by what the poller does with each. Pure apart from
    drawing each requeue's jittered delay from ``rng``."""
    plan = PollPlan()
    for outcome in statuses:
        requeues = outcome.state.requeues
        action = classify_poll_result(outcome.status, outcome.retryable, requeues, max_requeues)
        if action == PollAction.HARVEST:
            plan.done.append(outcome)
        elif action == PollAction.FAIL:
            error = poll_failure_message(outcome, max_requeues)
            plan.failed.append(FailedPoll(outcome, error, poll_attempt(outcome, False, now)))
        elif action == PollAction.REQUEUE:
            attempt = poll_attempt(outcome, True, now)
            plan.requeued.append(RequeuedPoll(outcome, backoff_seconds(requeues, rng), attempt))
        else:
            plan.running.append(outcome)
    return plan


def next_poll_delay(state: JobState, now: float, initial: float, cap: float) -> float:
    """How long until a still-running job's next status check: double the last gap,
    within ``[initial, cap]``. Pure."""
    current = (state.next_poll_at or now) - (state.last_poll or now)
    return min(max(current * 2, initial), cap)


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


async def drain_poll_queue(
    due: Iterable[str],
    *,
    concurrency: int,
    check: Callable[[str], Awaitable[PollOutcome]],
    process: Callable[[list[PollOutcome]], Awaitable[None]],
    stop: asyncio.Event | None = None,
) -> None:
    """Status-check ``due`` with a sliding window of at most ``concurrency`` in flight.

    Each completed check frees its slot immediately, so the next due job starts while the
    rest of the batch is still being polled; finished batches go to ``process`` (release
    the engine slot, requeue, fail) without blocking the other checks. A raised status
    check is logged and does not cancel its peers — the job stays in flight and the next
    pass retries it. ``stop`` stops refilling slots but lets active checks finish; if the
    caller is cancelled, every remaining check is cancelled and awaited.
    """
    if concurrency < 1:
        raise ValueError("poller poll concurrency must be at least 1")
    pending = iter(due)
    active: set[asyncio.Future[PollOutcome]] = set()

    def fill_slots() -> None:
        while len(active) < concurrency and not (stop is not None and stop.is_set()):
            job_id = next(pending, None)
            if job_id is None:
                return
            active.add(asyncio.ensure_future(check(job_id)))

    fill_slots()
    try:
        while active:
            completed, _ = await asyncio.wait(active, return_when=asyncio.FIRST_COMPLETED)
            active.difference_update(completed)
            statuses = _completed_checks(completed)
            fill_slots()
            if statuses:
                await process(statuses)
    finally:
        await _cancel_checks(active)


def _completed_checks(completed: set[asyncio.Future[PollOutcome]]) -> list[PollOutcome]:
    """The outcomes of finished checks; a check that raised is logged and left out."""
    statuses: list[PollOutcome] = []
    for future in completed:
        try:
            statuses.append(future.result())
        except Exception:
            logger.exception("Status check failed unexpectedly")
    return statuses


async def _cancel_checks(active: set[asyncio.Future[PollOutcome]]) -> None:
    for future in active:
        future.cancel()
    if active:
        await asyncio.gather(*active, return_exceptions=True)


def require_engine_job_id(state: JobState) -> str:
    """The engine's id for a job that was dispatched; LookupError for one that wasn't."""
    if state.engine_job_id is None:
        raise LookupError(f"job state has no engine_job_id (status {state.status})")
    return state.engine_job_id


async def probe_failure_status(adapter: EngineAdapter, engine_job_id: str) -> int | None:
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


def output_stats(run: EngineRun, alto: tuple[int, int] | None, txt: bytes | None) -> OutputStats:
    """Lines, blocks, characters and confidence of a harvested page. ``alto`` is the
    parsed ALTO's ``(blocks, chars)``, when the job produced one. Pure."""
    # Line count comes straight from the engine's status — it reports n_lines for every
    # job (computed during OCR), so lines are populated regardless of fmt.
    n_lines = run.metrics.get("n_lines")
    blocks, chars = alto if alto is not None else (None, None)
    # Prefer counting characters from the txt output (present for txt/multi jobs) so the
    # metric is populated regardless of fmt; count non-whitespace to match ALTO's char
    # semantics (sum of String CONTENT). ALTO's count stays for alto-only jobs.
    if txt is not None:
        chars = sum(1 for c in txt.decode("utf-8", "replace") if not c.isspace())
    reported_conf = run.metrics.get("mean_conf")
    return OutputStats(
        lines=n_lines if isinstance(n_lines, int) else None,
        blocks=blocks,
        chars=chars,
        mean_conf=float(reported_conf) if isinstance(reported_conf, int | float) else None,
    )
