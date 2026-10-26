"""Tests for scored dispatch (step 8 T5): allocate_scored is pure."""

from collections import Counter
from dataclasses import dataclass

from app.workers.submit.steps import allocate_scored, can_serve, expected_finish
from app.workers.submit.types import BackendSlot, PendingJob

ALTO_TXT = frozenset({"alto", "txt"})


@dataclass
class _Backend:
    id: int
    max_inflight: int = 4
    priority: int = 0


def _slot(
    id_: int,
    free: int = 30,
    inflight: int = 0,
    batches=None,
    max_inflight: int = 4,
    domains: dict[str, frozenset[str]] | None = None,
):
    return BackendSlot(
        backend=_Backend(id_, max_inflight),
        free=free,
        domains=domains if domains is not None else {"default": ALTO_TXT},
        inflight=inflight,
        batches=dict(batches or {}),
    )


def _job(
    i: int,
    batch: str | None = None,
    avoid=frozenset(),
    mp: float = 1.0,
    domain: str = "default",
    outputs: frozenset[str] = ALTO_TXT,
) -> PendingJob:
    return PendingJob(
        f"j{i}", float(i), domain, outputs, frozenset(avoid), megapixels=mp, batch_id=batch
    )


def _placed(assignments) -> dict[str, int]:
    """Job id → the backend it went to."""
    return {job.job_id: slot.backend.id for job, slot in assignments}


def _speeds(**by_backend: float):
    return lambda backend_id, domain: by_backend[f"b{backend_id}"]


def test_expected_finish() -> None:
    slot = _slot(1, inflight=3, max_inflight=4)
    slot.assigned = 1
    assert expected_finish(slot, _job(0, mp=2.0), 5.0) == (3 + 1 + 1) * 5.0 * 2.0 / 4


def test_twice_as_fast_gets_about_two_thirds() -> None:
    slots = [_slot(1), _slot(2)]
    assignments = allocate_scored(slots, [_job(i) for i in range(30)], _speeds(b1=1.0, b2=2.0), 0.0)
    assert len(assignments) == 30
    shares = Counter(slot.backend.id for _, slot in assignments)
    assert shares[1] == 20 and shares[2] == 10


def test_equal_backends_share_evenly() -> None:
    slots = [_slot(1), _slot(2), _slot(3)]
    jobs = [_job(i) for i in range(9)]
    assignments = allocate_scored(slots, jobs, _speeds(b1=1, b2=1, b3=1), 0.0)
    assert sorted(Counter(s.backend.id for _, s in assignments).values()) == [3, 3, 3]


def test_batch_sticks_to_its_backend_within_tolerance() -> None:
    slots = [_slot(1), _slot(2, batches={"B": 1})]
    # backend 2 is 10% slower: within a 15% tolerance, its batch keeps it
    assignments = allocate_scored(slots, [_job(0, batch="B")], _speeds(b1=1.0, b2=1.1), 0.15)
    assert assignments[0][1].backend.id == 2


def test_batch_affinity_gives_way_outside_tolerance() -> None:
    slots = [_slot(1), _slot(2, batches={"B": 1})]
    assignments = allocate_scored(slots, [_job(0, batch="B")], _speeds(b1=1.0, b2=2.0), 0.15)
    assert assignments[0][1].backend.id == 1


def test_avoid_beats_affinity() -> None:
    slots = [_slot(1), _slot(2, batches={"B": 3})]
    job = _job(0, batch="B", avoid={2})
    assignments = allocate_scored(slots, [job], _speeds(b1=1.0, b2=1.0), 0.15)
    assert assignments[0][1].backend.id == 1


def test_full_backend_is_never_chosen() -> None:
    slots = [_slot(1, free=0), _slot(2, free=1)]
    assignments = allocate_scored(slots, [_job(0), _job(1)], _speeds(b1=0.1, b2=5.0), 0.0)
    assert _placed(assignments) == {"j0": 2}  # j1 waits for a free slot


EQUAL = _speeds(b1=1.0, b2=1.0)


def test_a_domain_job_only_lands_on_a_backend_serving_it() -> None:
    slots = [_slot(1), _slot(2, domains={"kramarky": ALTO_TXT})]
    jobs = [_job(0, domain="kramarky"), _job(1, domain="kramarky")]
    assert _placed(allocate_scored(slots, jobs, EQUAL, 0.0)) == {"j0": 2, "j1": 2}


def test_a_job_no_backend_serves_stays_queued() -> None:
    assignments = allocate_scored([_slot(1)], [_job(0, domain="maps")], EQUAL, 0.0)
    assert assignments == []


def test_a_job_wanting_an_output_skips_a_backend_without_it() -> None:
    slots = [_slot(1, domains={"default": frozenset({"txt"})}), _slot(2)]
    assert _placed(allocate_scored(slots, [_job(0)], EQUAL, 0.0)) == {"j0": 2}


def test_an_avoided_backend_is_used_when_it_is_the_only_one_serving() -> None:
    slots = [_slot(1), _slot(2, domains={"kramarky": ALTO_TXT})]
    assert _placed(allocate_scored(slots, [_job(0, avoid={1})], EQUAL, 0.0)) == {"j0": 1}


def test_an_avoided_job_waits_for_a_full_alternative() -> None:
    slots = [_slot(1), _slot(2, free=0)]
    assert allocate_scored(slots, [_job(0, avoid={1})], EQUAL, 0.0) == []


def test_can_serve_needs_the_domain_and_every_output() -> None:
    slot = _slot(1)
    assert can_serve(slot, _job(0, outputs=frozenset({"alto"})))
    assert can_serve(slot, _job(1))
    assert not can_serve(slot, _job(2, outputs=frozenset({"alto", "json"})))
    assert not can_serve(slot, _job(3, domain="kramarky"))
