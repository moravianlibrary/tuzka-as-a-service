"""Tests for the round-robin dispatch allocator.

allocate_jobs is pure (no I/O), so these run without redis/DB. They pin the core
behaviors: even spread within a tier, capacity limits, domain-aware routing, and
avoid-backend logic (D7).
"""

from collections import Counter

from app.workers.submit import BackendSlot, PendingJob, allocate_jobs


class _Backend:
    """Minimal stand-in — the allocator only reads backend.id and backend.priority."""

    def __init__(self, id_: int, name: str) -> None:
        self.id = id_
        self.name = name


def _slot(
    name: str, free: int, domains: tuple[str, ...] = (), id_: int | None = None
) -> BackendSlot:
    return BackendSlot(backend=_Backend(id_ or 0, name), free=free, domains=set(domains))


def _job(
    job_id: str, score: float = 0.0, domain: str | None = None, avoid: frozenset[int] | None = None
) -> PendingJob:
    return PendingJob(job_id, score, domain, avoid or frozenset())


def test_spreads_evenly_across_equal_backends() -> None:
    slots = [_slot("a", 4), _slot("b", 4), _slot("c", 4)]
    jobs = [_job(f"j{i}", float(i)) for i in range(5)]

    assignments, leftovers = allocate_jobs(slots, jobs)

    assert not leftovers
    counts = Counter(slot.backend.name for _, slot in assignments)
    assert sorted(counts.values()) == [1, 2, 2]  # 2/2/1, not the greedy 4/1/0


def test_respects_capacity_and_returns_leftovers() -> None:
    slots = [_slot("a", 1), _slot("b", 1)]
    jobs = [_job(f"j{i}") for i in range(3)]

    assignments, leftovers = allocate_jobs(slots, jobs)

    assert len(assignments) == 2  # both single slots filled
    assert len(leftovers) == 1  # the third job has nowhere to go this pass


def test_domain_job_only_lands_on_a_serving_backend() -> None:
    slots = [_slot("cpu", 4), _slot("gpu", 4, domains=("handwriting",), id_=2)]
    jobs = [_job("j1", domain="handwriting"), _job("j2")]

    assignments, leftovers = allocate_jobs(slots, jobs)

    assert not leftovers
    placement = {pj.job_id: slot.backend.name for pj, slot in assignments}
    assert placement["j1"] == "gpu"  # domain-restricted job routed to the serving backend
    assert placement["j2"] == "cpu"  # domainless job takes the other


def test_unservable_domain_becomes_leftover() -> None:
    slots = [_slot("cpu", 4)]
    jobs = [_job("j1", domain="handwriting")]

    assignments, leftovers = allocate_jobs(slots, jobs)

    assert assignments == []
    assert leftovers == [_job("j1", domain="handwriting")]  # requeued for a tier that serves it


def test_no_slots_leaves_everything_for_requeue() -> None:
    assignments, leftovers = allocate_jobs([], [_job("j1", 1.0)])

    assert assignments == []
    assert leftovers == [_job("j1", 1.0)]


def test_avoided_backend_skipped_when_alternative_exists() -> None:
    """D7: a job avoiding backend 1 should land on backend 2."""
    slots = [_slot("a", 4, id_=1), _slot("b", 4, id_=2)]
    jobs = [_job("j1", avoid=frozenset({1}))]

    assignments, leftovers = allocate_jobs(slots, jobs)

    assert not leftovers
    pj, slot = assignments[0]
    assert pj.job_id == "j1"
    assert slot.backend.id == 2  # landed on the non-avoided backend


def test_avoided_backend_used_when_only_option() -> None:
    """D7 fallback: if all slots in the tier are avoided, use one anyway."""
    slots = [_slot("a", 4, id_=1)]
    jobs = [_job("j1", avoid=frozenset({1}))]

    assignments, leftovers = allocate_jobs(slots, jobs)

    assert not leftovers
    pj, slot = assignments[0]
    assert pj.job_id == "j1"
    assert slot.backend.id == 1  # only option, even though avoided


def test_avoided_backend_with_domain_fallback() -> None:
    """D7: non-avoided slot can't serve domain, fallback to avoided one."""
    slots = [
        _slot("a", 4, id_=1),  # not avoided but doesn't serve "handwriting"
        _slot("b", 4, domains=("handwriting",), id_=2),  # avoided AND serves domain
    ]
    jobs = [_job("j1", domain="handwriting", avoid=frozenset({2}))]

    assignments, leftovers = allocate_jobs(slots, jobs)

    assert not leftovers
    pj, slot = assignments[0]
    assert pj.job_id == "j1"
    assert slot.backend.id == 2  # non-avoided slot doesn't serve domain, fallback to avoided
