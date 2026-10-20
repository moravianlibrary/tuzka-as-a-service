"""Load arithmetic for ``GET /load`` (step 4): pure functions over the Redis counters.

The counters themselves are kept by the state functions in ``redis_jobs``; this module
only turns them into the totals an autoscaler reads.
"""

from __future__ import annotations

# Weight of the newest sample in the seconds-per-page average: low, so one slow page
# doesn't swing the backlog estimate an autoscaler acts on.
SPP_ALPHA = 0.1


def ewma(prev: float | None, sample: float, alpha: float = SPP_ALPHA) -> float:
    """Exponentially weighted moving average; the first sample is taken as-is."""
    if prev is None:
        return sample
    return alpha * sample + (1 - alpha) * prev
