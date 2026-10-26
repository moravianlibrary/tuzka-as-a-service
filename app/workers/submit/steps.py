"""The submit worker's stateless steps: which backend each pending job goes to, and
what a failed dispatch means for the job. Pure apart from the clock, so they're
unit-testable without the worker."""

from collections.abc import Callable
from typing import Any

import httpx

from app.clock import utcnow
from app.schemas.attempt import Attempt
from app.schemas.redis import JobState
from app.services.events import failure
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
) -> list[tuple[PendingJob, BackendSlot]]:
    """Give each job, in queue order, to the backend that would finish it first.

    ``spp(backend_id, domain)`` is the backend's seconds per megapixel (``perf``). A
    retried job's ``avoid`` set is applied before scoring, so a retry overrides affinity
    (D8), falling back to an avoided backend only when nothing else serves it. Among the
    backends within ``tolerance`` of the best, one already running pages of the job's
    batch wins (D7). Exact ties rotate, so equal backends share a burst evenly. Only
    backends with a free slot are chosen, so affinity can't overfill one (D9). Pure apart
    from updating the slots' ``free``, ``assigned`` and ``batches``.

    Returns each placed job with its slot; a job left out stays queued, for a lower tier
    or a backend that frees up.
    """
    assignments: list[tuple[PendingJob, BackendSlot]] = []
    order = {id(slot): i for i, slot in enumerate(slots)}
    start = 0
    for job in jobs:
        serving = [s for s in slots if can_serve(s, job)]
        allow_avoided = all(s.backend.id in job.avoid for s in serving)
        candidates = [
            s for s in serving if s.free > 0 and (allow_avoided or s.backend.id not in job.avoid)
        ]
        if not candidates:
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
    return assignments


def classify_dispatch_error(error: Exception) -> DispatchFailure:
    """What a dispatch exception means for the job. Pure."""
    return DispatchFailure(
        error=describe_exception(error),
        http_status=http_status_of(error),
        transient=is_retryable_dispatch_error(error),
        transport=isinstance(error, httpx.TransportError),
    )


def dispatch_event_fields(failed: DispatchFailure) -> dict[str, Any]:
    """The error columns of the job's ``retried``/``failed`` event."""
    return failure(FailureStage.DISPATCH, failed.http_status, failed.transport)


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
