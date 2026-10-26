"""Micro-benchmarks of the per-job / per-tick hot paths (plan 10, Part A).

Run with `make bench-micro`. Sizes are pinned in the names; record numbers with them.
"""

from app.services.domain_sync import plan_domain_links
from app.services.job_spec import options_errors
from app.services.load import compute_load, group_load
from app.workers.stages import StageTimer
from app.workers.submit.steps import allocate_scored
from app.workers.submit.types import BackendSlot

from .conftest import (
    ALTO_TXT,
    make_engine_info,
    make_jobs,
    make_load_counters,
    make_slots,
    tuzkaocr_schema,
)


def test_allocate_scored_10_backends_500_jobs(benchmark) -> None:
    slots, jobs = make_slots(10, free=40), make_jobs(500)

    def run() -> None:
        # allocate_scored updates the slots' counters, so each round gets fresh (cheap)
        # slots.
        allocate_scored(
            [BackendSlot(s.backend, s.free, s.domains) for s in slots],
            jobs,
            lambda backend_id, domain: 1.0,
            0.15,
        )

    benchmark(run)


def test_options_errors_valid(benchmark) -> None:
    schema = tuzkaocr_schema()
    options = {"alto_profile": "basic", "physical_img_nr": 3, "role_classifier": True}
    assert benchmark(options_errors, schema, options) == []


def test_options_errors_invalid(benchmark) -> None:
    schema = tuzkaocr_schema()
    options = {"physical_img_nr": "x", "colour": "red"}
    assert len(benchmark(options_errors, schema, options)) == 2


def test_plan_domain_links_4_domains(benchmark) -> None:
    schema = tuzkaocr_schema()
    info = make_engine_info(schema)
    known = {d: schema for d in info.domains}
    links, store, changed = benchmark(plan_domain_links, info, known)
    assert links and not store and not changed


def test_compute_load_100_fields(benchmark) -> None:
    queued = make_load_counters(25, 4)
    inflight = make_load_counters(25, 2)
    caps = {f"domain{d}": ALTO_TXT for d in range(0, 25, 2)}
    benchmark(compute_load, queued, inflight, {}, 10.0, None, caps)


def test_group_load_by_domain_100_fields(benchmark) -> None:
    queued = make_load_counters(25, 4)
    inflight = make_load_counters(25, 2)
    benchmark(group_load, queued, inflight, {}, 10.0, "domain", {})


def test_stage_timer_1000_empty_sections(benchmark) -> None:
    timer = StageTimer()

    def run() -> None:
        for _ in range(1000):
            with timer.stage("poll"):
                pass
        timer.flush()

    benchmark(run)


def test_stage_timer_baseline_1000_empty_sections(benchmark) -> None:
    def run() -> None:
        for _ in range(1000):
            pass

    benchmark(run)
