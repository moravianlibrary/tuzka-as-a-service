"""Tests for scored dispatch (step 8 T5): allocate_scored is pure."""

from collections import Counter
from dataclasses import dataclass

from app.workers.submit import BackendSlot, PendingJob, allocate_scored, expected_finish

ALTO_TXT = frozenset({"alto", "txt"})


@dataclass
class _Backend:
    id: int
    max_inflight: int = 4
    priority: int = 0


def _slot(id_: int, free: int = 30, inflight: int = 0, batches=None, max_inflight: int = 4):
    return BackendSlot(
        backend=_Backend(id_, max_inflight),
        free=free,
        domains={"default": ALTO_TXT},
        inflight=inflight,
        batches=dict(batches or {}),
    )


def _job(i: int, batch: str | None = None, avoid=frozenset(), mp: float = 1.0) -> PendingJob:
    return PendingJob(
        f"j{i}", float(i), "default", ALTO_TXT, frozenset(avoid), megapixels=mp, batch_id=batch
    )


def _speeds(**by_backend: float):
    return lambda backend_id, domain: by_backend[f"b{backend_id}"]


def test_expected_finish() -> None:
    slot = _slot(1, inflight=3, max_inflight=4)
    slot.assigned = 1
    assert expected_finish(slot, _job(0, mp=2.0), 5.0) == (3 + 1 + 1) * 5.0 * 2.0 / 4


def test_twice_as_fast_gets_about_two_thirds() -> None:
    slots = [_slot(1), _slot(2)]
    assignments, leftovers = allocate_scored(
        slots, [_job(i) for i in range(30)], _speeds(b1=1.0, b2=2.0), 0.0
    )
    assert not leftovers
    shares = Counter(slot.backend.id for _, slot in assignments)
    assert shares[1] == 20 and shares[2] == 10


def test_equal_backends_share_evenly() -> None:
    slots = [_slot(1), _slot(2), _slot(3)]
    jobs = [_job(i) for i in range(9)]
    assignments, _ = allocate_scored(slots, jobs, _speeds(b1=1, b2=1, b3=1), 0.0)
    assert sorted(Counter(s.backend.id for _, s in assignments).values()) == [3, 3, 3]


def test_batch_sticks_to_its_backend_within_tolerance() -> None:
    slots = [_slot(1), _slot(2, batches={"B": 1})]
    # backend 2 is 10% slower: within a 15% tolerance, its batch keeps it
    assignments, _ = allocate_scored(slots, [_job(0, batch="B")], _speeds(b1=1.0, b2=1.1), 0.15)
    assert assignments[0][1].backend.id == 2


def test_batch_affinity_gives_way_outside_tolerance() -> None:
    slots = [_slot(1), _slot(2, batches={"B": 1})]
    assignments, _ = allocate_scored(slots, [_job(0, batch="B")], _speeds(b1=1.0, b2=2.0), 0.15)
    assert assignments[0][1].backend.id == 1


def test_avoid_beats_affinity() -> None:
    slots = [_slot(1), _slot(2, batches={"B": 3})]
    job = _job(0, batch="B", avoid={2})
    assignments, _ = allocate_scored(slots, [job], _speeds(b1=1.0, b2=1.0), 0.15)
    assert assignments[0][1].backend.id == 1


def test_full_backend_is_never_chosen() -> None:
    slots = [_slot(1, free=0), _slot(2, free=1)]
    assignments, leftovers = allocate_scored(
        slots, [_job(0), _job(1)], _speeds(b1=0.1, b2=5.0), 0.0
    )
    assert [s.backend.id for _, s in assignments] == [2]
    assert [j.job_id for j in leftovers] == ["j1"]
