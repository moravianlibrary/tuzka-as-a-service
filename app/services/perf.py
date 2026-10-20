"""Measured engine speed per (backend, domain), for dispatch (step 8).

The score is engine seconds per megapixel: an average of what the backend actually took
(D1), made worse by failures (D6) so a flaky backend gets less work without anyone
disabling it. Kept in Redis, ``perf:{backend_id}``, three fields per domain:

    {domain}       average seconds per megapixel of finished pages
    {domain}:n     how many pages that average is over
    {domain}:pen   failure penalty factor, ×1.5 per failure (capped), eased on success
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable

import redis.asyncio as aioredis

ALPHA = 0.2  # weight of the newest page in the average
COLD_SAMPLES = 5  # below this, a backend borrows the other backends' median (D3)
PENALTY = 1.5  # per failed attempt (D6)
MAX_PENALTY = 10.0
MIN_MEGAPIXELS = 0.01  # a thumbnail doesn't make the per-MP figure explode

Scores = dict[tuple[int, str], tuple[float, int, float]]  # (spp, samples, penalty)


def _key(backend_id: int) -> str:
    return f"perf:{backend_id}"


def ewma(prev: float | None, sample: float, alpha: float = ALPHA) -> float:
    if prev is None:
        return sample
    return alpha * sample + (1 - alpha) * prev


def penalize(penalty: float) -> float:
    """The penalty factor after one more failure."""
    return min(penalty * PENALTY, MAX_PENALTY)


def ease(penalty: float) -> float:
    """The penalty factor after a page finished fine: failures fade as it recovers."""
    return max(1.0, penalty / PENALTY)


def effective_spp(backend_id: int, domain: str, scores: Scores, fallback: float) -> float:
    """Seconds per megapixel to plan with for (backend, domain), penalty included.

    A backend with enough pages uses its own average. A cold one borrows the median of
    the others that have enough (D3); with none, its own few pages, else ``fallback``.
    """
    own = scores.get((backend_id, domain))
    penalty = own[2] if own else 1.0
    if own and own[1] >= COLD_SAMPLES:
        return own[0] * penalty
    warm = [
        spp
        for (other, dom), (spp, samples, _) in scores.items()
        if dom == domain and other != backend_id and samples >= COLD_SAMPLES
    ]
    if warm:
        return statistics.median(warm) * penalty
    if own and own[1] > 0:
        return own[0] * penalty
    return fallback * penalty


async def record_service(
    r: aioredis.Redis, backend_id: int, domain: str, seconds: float, megapixels: float
) -> None:
    """Fold one finished page into the backend's average for the domain, and ease its
    failure penalty. Two pollers racing can drop a sample; for an average that's fine."""
    sample = seconds / max(megapixels, MIN_MEGAPIXELS)
    prev, pen = await r.hmget(_key(backend_id), [domain, f"{domain}:pen"])
    async with r.pipeline(transaction=False) as pipe:
        pipe.hset(_key(backend_id), domain, ewma(_f(prev), sample))
        pipe.hincrby(_key(backend_id), f"{domain}:n", 1)
        if pen is not None:
            pipe.hset(_key(backend_id), f"{domain}:pen", ease(float(pen)))
        await pipe.execute()


async def record_failure(r: aioredis.Redis, backend_id: int, domain: str) -> None:
    """A failed attempt on the backend for the domain makes it look slower (D6)."""
    pen = await r.hget(_key(backend_id), f"{domain}:pen")
    await r.hset(_key(backend_id), f"{domain}:pen", penalize(_f(pen) or 1.0))


async def load_scores(
    r: aioredis.Redis, backend_ids: Iterable[int], domains: Iterable[str]
) -> Scores:
    """Every (backend, domain) score in one round trip, for one dispatch tick."""
    ids, names = list(backend_ids), sorted(set(domains))
    fields = [f for d in names for f in (d, f"{d}:n", f"{d}:pen")]
    if not ids or not fields:
        return {}
    async with r.pipeline(transaction=False) as pipe:
        for backend_id in ids:
            pipe.hmget(_key(backend_id), fields)
        rows = await pipe.execute()
    scores: Scores = {}
    for backend_id, values in zip(ids, rows):
        for i, domain in enumerate(names):
            spp, samples, pen = values[3 * i : 3 * i + 3]
            if spp is not None:
                scores[(backend_id, domain)] = (float(spp), int(samples or 0), _f(pen) or 1.0)
            elif pen is not None:
                scores[(backend_id, domain)] = (0.0, 0, float(pen))
    return scores


def _f(value: bytes | str | None) -> float | None:
    return float(value) if value is not None else None
