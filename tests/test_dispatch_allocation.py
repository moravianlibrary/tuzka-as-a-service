"""Tests for the round-robin dispatch allocator.

allocate_jobs is pure (no I/O), so these run without redis/DB. They pin the core
behaviors: even spread within a tier, capacity limits, and domain-aware routing.
"""

from collections import Counter

from app.workers.submit import BackendSlot, allocate_jobs


class _Backend:
    """Minimal stand-in — the allocator only reads back the object it was given."""

    def __init__(self, name: str) -> None:
        self.name = name


def _slot(name: str, free: int, domains: tuple[str, ...] = ()) -> BackendSlot:
    return BackendSlot(backend=_Backend(name), free=free, domains=set(domains))


def test_spreads_evenly_across_equal_backends() -> None:
    slots = [_slot("a", 4), _slot("b", 4), _slot("c", 4)]
    jobs = [(f"j{i}", float(i), None) for i in range(5)]

    assignments, leftovers = allocate_jobs(slots, jobs)

    assert not leftovers
    counts = Counter(slot.backend.name for _, _, slot in assignments)
    assert sorted(counts.values()) == [1, 2, 2]  # 2/2/1, not the greedy 4/1/0


def test_respects_capacity_and_returns_leftovers() -> None:
    slots = [_slot("a", 1), _slot("b", 1)]
    jobs = [(f"j{i}", 0.0, None) for i in range(3)]

    assignments, leftovers = allocate_jobs(slots, jobs)

    assert len(assignments) == 2  # both single slots filled
    assert len(leftovers) == 1  # the third job has nowhere to go this pass


def test_domain_job_only_lands_on_a_serving_backend() -> None:
    slots = [_slot("cpu", 4), _slot("gpu", 4, domains=("handwriting",))]
    jobs = [("j1", 0.0, "handwriting"), ("j2", 0.0, None)]

    assignments, leftovers = allocate_jobs(slots, jobs)

    assert not leftovers
    placement = {job_id: slot.backend.name for job_id, _, slot in assignments}
    assert placement["j1"] == "gpu"  # domain-restricted job routed to the serving backend
    assert placement["j2"] == "cpu"  # domainless job takes the other


def test_unservable_domain_becomes_leftover() -> None:
    slots = [_slot("cpu", 4)]
    jobs = [("j1", 0.0, "handwriting")]

    assignments, leftovers = allocate_jobs(slots, jobs)

    assert assignments == []
    assert leftovers == [("j1", 0.0)]  # requeued for a tier that serves it


def test_no_slots_leaves_everything_for_requeue() -> None:
    assignments, leftovers = allocate_jobs([], [("j1", 1.0, None)])

    assert assignments == []
    assert leftovers == [("j1", 1.0)]
