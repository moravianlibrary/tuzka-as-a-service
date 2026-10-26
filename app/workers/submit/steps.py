"""Dispatch allocation: which backend each pending job goes to. Pure, so it's
unit-testable without the worker."""

from collections.abc import Callable

import httpx

from app.clock import utcnow
from app.schemas.attempt import Attempt
from app.schemas.redis import JobState
from app.services.redis_jobs import (
    QueueEntry,
)
from app.services.retry_policy import (
    describe_exception,
    http_status_of,
    is_retryable_dispatch_error,
)
from app.vocabulary import FailureStage
from app.workers.submit.types import BackendSlot, DispatchFailure, PendingJob


def pending_job(entry: QueueEntry, state: JobState) -> PendingJob:
    """A peeked job as dispatch sees it."""
    return PendingJob(
        entry.job_id,
        entry.score,
        state.domain,
        state.outputs,
        state.avoid_backends,
        megapixels=state.megapixels,
        batch_id=state.batch_id,
    )


def serves(domains: dict[str, frozenset[str]], domain: str, outputs: frozenset[str]) -> bool:
    """Whether a backend serving ``domains`` can run a ``domain`` job wanting ``outputs``."""
    return domain in domains and outputs <= domains[domain]


def unstored_capacity(limit: int, inflight: int, storing: int) -> int:
    """Dispatch credits left until results already in progress are stored."""
    return max(0, limit - inflight - storing)


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


def expected_finish(slot: BackendSlot, job: PendingJob, seconds_per_mp: float) -> float:
    """When the job would finish on the backend, in seconds from now (D4): the pages
    ahead of it plus itself, at the backend's measured speed for the job's size,
    spread over its parallel slots."""
    queue = slot.inflight + slot.assigned + 1
    return queue * seconds_per_mp * job.megapixels / max(1, slot.backend.max_inflight)


def allocate_scored(
    slots: list[BackendSlot],
    jobs: list[PendingJob],
    spp: Callable[[int, str], float],
    tolerance: float,
) -> tuple[list[tuple[PendingJob, BackendSlot]], list[PendingJob]]:
    """Give each job, in queue order, to the backend that would finish it first.

    ``spp(backend_id, domain)`` is the backend's seconds per megapixel (``perf``). A
    retried job's ``avoid`` set is applied before scoring, so a retry overrides affinity
    (D8), falling back to an avoided backend only when nothing else serves it. Among the
    backends within ``tolerance`` of the best, one already running pages of the job's
    batch wins (D7). Exact ties rotate, so equal backends share a burst evenly. Only
    backends with a free slot are chosen, so affinity can't overfill one (D9). Pure apart
    from updating the slots' ``free``, ``assigned`` and ``batches``.

    Returns ``(assignments, leftovers)`` like ``allocate_jobs``.
    """
    assignments: list[tuple[PendingJob, BackendSlot]] = []
    leftovers: list[PendingJob] = []
    order = {id(slot): i for i, slot in enumerate(slots)}
    start = 0
    for job in jobs:
        serving = [s for s in slots if can_serve(s, job)]
        allow_avoided = all(s.backend.id in job.avoid for s in serving)
        candidates = [
            s for s in serving if s.free > 0 and (allow_avoided or s.backend.id not in job.avoid)
        ]
        if not candidates:
            leftovers.append(job)
            continue
        finish = {id(s): expected_finish(s, job, spp(s.backend.id, job.domain)) for s in candidates}
        best = min(finish.values())
        near = [s for s in candidates if finish[id(s)] <= best * (1 + tolerance)]
        affine = [s for s in near if job.batch_id and s.batches.get(job.batch_id, 0) > 0]
        if affine:
            pool_best = min(finish[id(s)] for s in affine)
            pool = [s for s in affine if finish[id(s)] == pool_best]
        else:
            pool = [s for s in candidates if finish[id(s)] == best]
        # Rotate among exact ties, starting after the last backend chosen.
        slot = min(pool, key=lambda s: (order[id(s)] - start) % len(slots))
        start = (order[id(slot)] + 1) % len(slots)
        slot.free -= 1
        slot.assigned += 1
        if job.batch_id:
            slot.batches[job.batch_id] = slot.batches.get(job.batch_id, 0) + 1
        assignments.append((job, slot))
    return assignments, leftovers


def classify_dispatch_error(error: Exception) -> DispatchFailure:
    """What a dispatch exception means for the job. Pure."""
    return DispatchFailure(
        error=describe_exception(error),
        http_status=http_status_of(error),
        transient=is_retryable_dispatch_error(error),
        transport=isinstance(error, httpx.TransportError),
    )


def failed_dispatch_attempt(backend_id: int, http_status: int | None, error: str) -> Attempt:
    """The ``jobs.attempts`` entry for a dispatch that fails the job. Pure apart from the
    clock."""
    return Attempt(
        at=utcnow(),
        stage=FailureStage.DISPATCH,
        backend_id=backend_id,
        http_status=http_status,
        error=error,
        retry=False,
    )


def retried_dispatch_attempt(backend_id: int, http_status: int | None, error: str) -> Attempt:
    """The ``jobs.attempts`` entry for a dispatch that's retried on another backend."""
    return failed_dispatch_attempt(backend_id, http_status, error).model_copy(
        update={"retry": True}
    )
