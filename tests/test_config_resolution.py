"""Pins runtime config resolution: defaults, partial/full overrides, malformed values,
and the state TTL derivation."""

from unittest.mock import AsyncMock

import pytest

from app.schemas.api.user import EffectiveLimits, UserLimitOverrides
from app.schemas.runtime_config import FALLBACK_RATE_LIMITS, RateLimit, RuntimeConfig
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


def test_runtime_config_defaults_when_absent():
    config = RuntimeConfig.from_rows({})

    assert config.queued_timeout_seconds == 900
    assert config.running_timeout_seconds == 300
    assert config.max_requeues == 5
    assert config.results_max_bytes is None
    assert config.rate_limits == FALLBACK_RATE_LIMITS


def test_runtime_config_reads_rows_by_key():
    config = RuntimeConfig.from_rows(
        {
            "presigned.ttl_minutes": 15,
            "dispatch.affinity_tolerance": "0.3",
            "storage.incoming_ttl_minutes": 30,
            "rate_limit.ws_connect": {"burst": 4},
            "unrelated.key": "ignored",
        }
    )

    assert config.presigned_ttl_minutes == 15
    assert config.affinity_tolerance == 0.3
    assert config.storage_ttl_minutes("incoming") == 30
    assert config.storage_ttl_minutes("results") == 60  # no row for that bucket
    assert config.rate_limits[LimitClass.WS_CONNECT] == RateLimit(per_minute=5, burst=4)


def test_malformed_value_keeps_its_default_only():
    config = RuntimeConfig.from_rows({"jobs.max_requeues": "many", "presigned.ttl_minutes": 15})

    assert config.max_requeues == 5
    assert config.presigned_ttl_minutes == 15  # the rest of the table still applies


@pytest.mark.asyncio
async def test_state_ttl_is_queued_plus_longest_running_plus_margin(monkeypatch):
    config = RuntimeConfig.from_rows(
        {"jobs.queued_timeout_seconds": 900, "jobs.running_timeout_seconds": 300}
    )
    domains = {"maps": cfg.DomainLimits(running_timeout_seconds=1200)}
    monkeypatch.setattr(cfg, "get_runtime_config", AsyncMock(return_value=config))
    monkeypatch.setattr(cfg, "get_domain_limits", AsyncMock(return_value=domains))

    assert await cfg.get_state_ttl_seconds(None) == 900 + 1200 + 60
