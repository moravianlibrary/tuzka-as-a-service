"""Load arithmetic for ``GET /load`` (step 4): pure functions over the Redis counters.

The counters themselves are kept by the state functions in ``redis_jobs``; this module
only turns them into the totals an autoscaler reads.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

from app.vocabulary import LoadGroupBy

# Weight of the newest sample in the seconds-per-page average: low, so one slow page
# doesn't swing the backlog estimate an autoscaler acts on.
SPP_ALPHA = 0.1


def load_field(domain: str, outputs: frozenset[str]) -> str:
    """The load-counter field for a (domain, outputs) pair, e.g. ``default|alto,txt``."""
    return f"{domain}|{','.join(sorted(outputs))}"


def parse_load_field(field: str) -> tuple[str, frozenset[str]]:
    """Inverse of ``load_field``."""
    domain, _, outputs = field.partition("|")
    return domain, frozenset(o for o in outputs.split(",") if o)


def ewma(prev: float | None, sample: float, alpha: float = SPP_ALPHA) -> float:
    """Exponentially weighted moving average; the first sample is taken as-is."""
    if prev is None:
        return sample
    return alpha * sample + (1 - alpha) * prev


@dataclass(frozen=True)
class LoadTotals:
    queued: int = 0
    inflight: int = 0
    # Seconds of engine work not finished yet: (queued + in-flight pages) × seconds per
    # page. In-flight pages count so an autoscaler doesn't scale down engines that are
    # still working once the queue empties.
    backlog_s: float = 0.0

    def as_dict(self) -> dict[str, int | float]:
        return {"queued": self.queued, "inflight": self.inflight, "backlog_s": self.backlog_s}


def _counts(
    queued: dict[str, int], inflight: dict[str, int]
) -> Iterator[tuple[str, frozenset[str], int, int]]:
    """``(domain, outputs, queued, inflight)`` per counter field."""
    for field in queued.keys() | inflight.keys():
        domain, outputs = parse_load_field(field)
        yield domain, outputs, queued.get(field, 0), inflight.get(field, 0)


def _matches(
    domain: str,
    outputs: frozenset[str],
    domains: set[str] | None,
    caps: dict[str, frozenset[str]] | None,
) -> bool:
    """Whether a field counts under the filters: its domain is asked for (when
    ``domains`` is given), and, when ``caps`` is given, a workload with those
    capabilities could serve it (domain served, outputs covered)."""
    if domains is not None and domain not in domains:
        return False
    if caps is not None:
        return domain in caps and outputs <= caps[domain]
    return True


def compute_load(
    queued: dict[str, int],
    inflight: dict[str, int],
    spp: dict[str, float],
    default_spp: float,
    domains: set[str] | None = None,
    caps: dict[str, frozenset[str]] | None = None,
) -> LoadTotals:
    """Total load over the counter fields that pass the filters. ``spp`` is seconds per
    page by domain; a domain without a measurement uses ``default_spp``."""
    total_queued = total_inflight = 0
    backlog = 0.0
    for domain, outputs, q, i in _counts(queued, inflight):
        if not _matches(domain, outputs, domains, caps):
            continue
        # Counters can dip below zero briefly between a crash and the drift repair.
        q, i = max(q, 0), max(i, 0)
        total_queued += q
        total_inflight += i
        backlog += (q + i) * spp.get(domain, default_spp)
    return LoadTotals(total_queued, total_inflight, backlog)


def group_load(
    queued: dict[str, int],
    inflight: dict[str, int],
    spp: dict[str, float],
    default_spp: float,
    by: LoadGroupBy,
    adapter_caps: dict[str, dict[str, frozenset[str]]],
    domains: set[str] | None = None,
) -> dict[str, LoadTotals]:
    """Load per domain, or per adapter. ``adapter_caps`` maps adapter → its
    capabilities (and limits the adapters reported). With ``by="adapter"``, a job some
    adapters can all serve counts in each of their groups, so groups can overlap."""
    if by == LoadGroupBy.ADAPTER:
        return {
            adapter: compute_load(queued, inflight, spp, default_spp, domains, caps)
            for adapter, caps in sorted(adapter_caps.items())
        }
    names = {d for d, _, _, _ in _counts(queued, inflight)}
    return {
        name: compute_load(queued, inflight, spp, default_spp, {name}, None)
        for name in sorted(names)
        if domains is None or name in domains
    }
