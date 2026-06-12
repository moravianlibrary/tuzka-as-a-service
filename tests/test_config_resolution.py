import pytest

from app.services import config as cfg
from app.services.config import (
    FALLBACK_LIMITS,
    ClassLimits,
    parse_default_limits,
    resolve,
)


def test_resolve_no_overrides_uses_default():
    default = ClassLimits(per_minute=100, burst=5)
    assert resolve(None, None, default) == default


def test_resolve_partial_override():
    default = ClassLimits(per_minute=100, burst=5)
    assert resolve(10, None, default) == ClassLimits(per_minute=10, burst=5)
    assert resolve(None, 9, default) == ClassLimits(per_minute=100, burst=9)


def test_resolve_full_override():
    default = ClassLimits(per_minute=100, burst=5)
    assert resolve(10, 1, default) == ClassLimits(per_minute=10, burst=1)


def test_parse_default_limits_valid():
    parsed = parse_default_limits({"per_minute": 30, "burst": 3}, FALLBACK_LIMITS["query"])
    assert parsed == ClassLimits(per_minute=30, burst=3)


def test_parse_default_limits_missing_or_malformed_falls_back():
    fb = FALLBACK_LIMITS["submit"]
    assert parse_default_limits(None, fb) == fb
    assert parse_default_limits("garbage", fb) == fb
    assert parse_default_limits({"per_minute": 30}, fb) == ClassLimits(
        per_minute=30, burst=fb.burst
    )


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
    db = _FakeDB({"jobs.retention_days": 30, "presigned.ttl_minutes": 15})
    assert await cfg.get_job_retention_days(db) == 30
    cfg._cache.clear()
    assert await cfg.get_presigned_ttl_minutes(db) == 15


@pytest.mark.asyncio
async def test_max_requeues_default_and_override():
    cfg._cache.clear()
    assert await cfg.get_max_requeues(_FakeDB({})) == 3
    cfg._cache.clear()
    assert await cfg.get_max_requeues(_FakeDB({"jobs.max_requeues": 5})) == 5
