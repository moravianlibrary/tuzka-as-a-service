"""Builders for the hot-path micro-benchmarks (plan 10, Part A).

Each benchmark pins the input size it was measured at, so numbers recorded in a plan's
`> Verified:` stay comparable. Builders are deterministic (seeded), never random per run.
"""

import json
import random
from pathlib import Path
from typing import Any

from app.engines.base import DomainInfo, EngineInfo
from app.engines.openapi import options_schema_from_openapi
from app.services.load import load_field
from app.workers.submit.types import BackendSlot, PendingJob

FIXTURES = Path(__file__).parent.parent / "fixtures"
ALTO_TXT = frozenset({"alto", "txt"})
OCR_DOMAINS = ("default", "kramarky", "handwritten", "kurrent")


class FakeBackend:
    """The allocator reads only ``id``, ``priority`` and ``max_inflight``."""

    def __init__(self, id_: int) -> None:
        self.id = id_
        self.priority = 0
        self.max_inflight = 4


def tuzkaocr_schema() -> dict[str, Any]:
    spec = json.loads((FIXTURES / "tuzkaocr_openapi.json").read_text())
    schema = options_schema_from_openapi(spec)
    assert schema is not None
    return schema


def make_slots(n: int, free: int = 8) -> list[BackendSlot]:
    """``n`` backends: every one serves default, every other one the other OCR domains."""
    slots = []
    for i in range(n):
        names = OCR_DOMAINS if i % 2 == 0 else ("default",)
        domains = {d: ALTO_TXT for d in names}
        backend: Any = FakeBackend(i + 1)
        slots.append(BackendSlot(backend=backend, free=free, domains=domains))
    return slots


def make_jobs(n: int, seed: int = 42) -> list[PendingJob]:
    """``n`` jobs, mostly default, some other domains, a few retries avoiding a backend."""
    rng = random.Random(seed)
    jobs = []
    for i in range(n):
        domain = "default" if rng.random() < 0.7 else rng.choice(OCR_DOMAINS[1:])
        avoid = frozenset({rng.randint(1, 10)}) if rng.random() < 0.05 else frozenset()
        jobs.append(PendingJob(f"job-{i}", float(i), domain, ALTO_TXT, avoid))
    return jobs


def make_engine_info(schema: dict[str, Any]) -> EngineInfo:
    return EngineInfo(
        domains={d: DomainInfo(ALTO_TXT, schema) for d in OCR_DOMAINS}, version="1.7.0"
    )


def make_load_counters(n_domains: int, n_output_sets: int) -> dict[str, int]:
    """Counter fields for ``n_domains`` × ``n_output_sets`` (domain, outputs) pairs."""
    output_sets = [frozenset({"alto", "txt"}), frozenset({"alto"}), frozenset({"txt"})]
    output_sets += [frozenset({f"out{i}"}) for i in range(max(0, n_output_sets - 3))]
    return {
        load_field(f"domain{d}", outs): d + 1
        for d in range(n_domains)
        for outs in output_sets[:n_output_sets]
    }
