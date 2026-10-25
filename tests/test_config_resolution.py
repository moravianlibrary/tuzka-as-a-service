"""Pins runtime config resolution: defaults, partial/full overrides, and TTL/timeout/requeue derivations."""

import pytest

from app.schemas.api.user import EffectiveLimits, UserLimitOverrides
from app.schemas.runtime_config import FALLBACK_RATE_LIMITS, RateLimit
from app.services import config as cfg
from app.vocabulary import LimitClass

DEFAULT = RateLimit(per_minute=100, burst=5)


def test_no_overrides_uses_default():
    assert UserLimitOverrides().apply(LimitClass.QUERY, DEFAULT) == DEFAULT


def test_partial_override():
    assert DEFAULT.overridden(10, None) == RateLimit(per_minute=10, burst=5)
    assert DEFAULT.overridden(None, 9) == RateLimit(per_minute=100, burst=9)


def test_override_applies_to_its_own_class_only():
    overrides = UserLimitOverrides(rate_ws_per_minute=10, burst_ws=1)

    assert overrides.apply(LimitClass.WS_CONNECT, DEFAULT) == RateLimit(per_minute=10, burst=1)
    assert overrides.apply(LimitClass.SUBMIT, DEFAULT) == DEFAULT


def test_rate_limit_from_config_row():
    parsed = RateLimit.from_config(
        {"per_minute": 30, "burst": 3}, FALLBACK_RATE_LIMITS[LimitClass.QUERY]
    )
    assert parsed == RateLimit(per_minute=30, burst=3)


def test_rate_limit_missing_or_malformed_falls_back():
    fb = FALLBACK_RATE_LIMITS[LimitClass.SUBMIT]
    assert RateLimit.from_config(None, fb) == fb
    assert RateLimit.from_config("garbage", fb) == fb
    assert RateLimit.from_config({"per_minute": "lots"}, fb) == fb
    assert RateLimit.from_config({"per_minute": 30}, fb) == RateLimit(per_minute=30, burst=fb.burst)


def test_effective_limits_flatten_to_the_override_fields():
    effective = EffectiveLimits.from_limits(FALLBACK_RATE_LIMITS)

    assert effective.rate_ws_per_minute == 5
    assert effective.burst_ws == 2
    assert effective.burst_submit == 10


class _FakeDB:
    """Minimal stand-in: config_service.get_value only calls db.execute()."""

    def __init__(self, rows: dict):
        self._rows = rows

    async def execute(self, stmt):
        key = stmt.compile().params.get("key_1")
        value = self._rows.get(key)

        class _Entry:
            def __init__(self, v):
                self.value = v

        class _Result:
            def all(self_inner):
                return []  # no per-domain limit overrides

            def scalar_one_or_none(self_inner):
                return _Entry(value) if value is not None else None

        return _Result()


@pytest.mark.asyncio
async def test_job_timeout_defaults_when_absent():
    cfg._cache.clear()
    db = _FakeDB({})
    assert await cfg.get_job_queued_timeout_seconds(db) == 900
    cfg._cache.clear()
    assert await cfg.get_job_running_timeout_seconds(db) == 300


@pytest.mark.asyncio
async def test_state_ttl_is_queued_plus_running_plus_margin():
    cfg._cache.clear()
    db = _FakeDB({"jobs.queued_timeout_seconds": 900, "jobs.running_timeout_seconds": 300})
    assert await cfg.get_state_ttl_seconds(db) == 900 + 300 + 60


@pytest.mark.asyncio
async def test_overrides_from_config_rows():
    cfg._cache.clear()
    db = _FakeDB({"presigned.ttl_minutes": 15})
    assert await cfg.get_presigned_ttl_minutes(db) == 15


@pytest.mark.asyncio
async def test_max_requeues_default_and_override():
    cfg._cache.clear()
    assert await cfg.get_max_requeues(_FakeDB({})) == 5
    cfg._cache.clear()
    assert await cfg.get_max_requeues(_FakeDB({"jobs.max_requeues": 5})) == 5
