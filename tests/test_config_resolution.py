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
