"""Tests for the dispatch speed scores (step 8): the pure rules, and a Redis round trip
with the ``redis_client`` fixture (skips if Redis is unreachable)."""

import pytest
import redis.asyncio as aioredis

from app.services.perf import (
    MAX_PENALTY,
    Score,
    ease,
    effective_spp,
    load_scores,
    penalize,
    record_failure,
    record_service,
)


def test_penalty_grows_capped_and_eases() -> None:
    assert penalize(1.0) == 1.5
    assert penalize(9.0) == MAX_PENALTY
    assert ease(1.5) == 1.0
    assert ease(1.0) == 1.0


def test_warm_backend_uses_its_own_average() -> None:
    scores = {(1, "default"): Score(2.0, 10, 1.0), (2, "default"): Score(4.0, 10, 1.0)}
    assert effective_spp(1, "default", scores, 99.0) == 2.0


def test_cold_backend_borrows_the_median() -> None:
    scores = {
        (1, "default"): Score(2.0, 10, 1.0),
        (2, "default"): Score(4.0, 10, 1.0),
        (3, "default"): Score(6.0, 10, 1.0),
        (4, "default"): Score(100.0, 2, 1.0),  # cold
    }
    assert effective_spp(4, "default", scores, 99.0) == 4.0
    assert effective_spp(5, "default", scores, 99.0) == 4.0  # never seen


def test_nothing_measured_uses_the_fallback_with_the_penalty() -> None:
    assert effective_spp(1, "maps", {}, 10.0) == 10.0
    assert effective_spp(1, "maps", {(1, "maps"): Score(0.0, 0, 1.5)}, 10.0) == 15.0


def test_penalty_multiplies_the_score() -> None:
    scores = {(1, "default"): Score(2.0, 10, 3.0)}
    assert effective_spp(1, "default", scores, 99.0) == 6.0


@pytest.mark.asyncio
async def test_round_trip(redis_client: aioredis.Redis) -> None:
    await record_service(redis_client, 1, "default", seconds=10.0, megapixels=5.0)  # 2 s/MP
    await record_service(redis_client, 1, "default", seconds=20.0, megapixels=5.0)  # 4 s/MP
    await record_failure(redis_client, 2, "default")
    scores = await load_scores(redis_client, [1, 2, 3], ["default"])
    score = scores[(1, "default")]
    assert score.spp == pytest.approx(0.2 * 4 + 0.8 * 2)
    assert (score.samples, score.penalty) == (2, 1.0)
    assert scores[(2, "default")] == Score(0.0, 0, 1.5)
    assert (3, "default") not in scores
