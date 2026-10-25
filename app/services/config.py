"""Cached accessors for runtime config (TTLs, timeouts, rate limits) from Postgres."""

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config import ConfigEntry
from app.models.domain import Domain
from app.models.user import User
from app.schemas.api.user import UserLimitOverrides
from app.schemas.runtime_config import RateLimit, RuntimeConfig
from app.vocabulary import LimitClass

# Short in-process cache: hot path must not hit Postgres per request,
# admin changes still take effect within seconds on every replica.
# The user: key space is bounded because callers only pass authenticated
# usernames (require_user gates the hot path), so this dict cannot grow
# unboundedly from unauthenticated traffic.
CACHE_TTL_SECONDS = 10.0
_cache: dict[str, tuple[Any, float]] = {}

STATE_TTL_MARGIN_SECONDS = 60


def _cache_get(key: str) -> tuple[Any, bool]:
    hit = _cache.get(key)
    if hit is not None and time.monotonic() - hit[1] < CACHE_TTL_SECONDS:
        return hit[0], True
    return None, False


def _cache_put(key: str, value: Any) -> None:
    _cache[key] = (value, time.monotonic())


async def get_runtime_config(db: AsyncSession) -> RuntimeConfig:
    """The whole config table, parsed; one small query per cache period."""
    cached, ok = _cache_get("runtime_config")
    if ok:
        config: RuntimeConfig = cached
        return config
    config = RuntimeConfig.from_rows(await get_all(db))
    _cache_put("runtime_config", config)
    return config


async def get_all(db: AsyncSession) -> dict[str, Any]:
    result = await db.execute(select(ConfigEntry).order_by(ConfigEntry.key))
    return {e.key: e.value for e in result.scalars().all()}


async def set_values(db: AsyncSession, values: dict[str, Any]) -> None:
    result = await db.execute(select(ConfigEntry).where(ConfigEntry.key.in_(values)))
    existing = {entry.key: entry for entry in result.scalars().all()}
    for key, value in values.items():
        entry = existing.get(key)
        if entry:
            entry.value = value
        else:
            db.add(ConfigEntry(key=key, value=value))
    await db.commit()
    _cache.pop("runtime_config", None)


def invalidate_user(username: str) -> None:
    _cache.pop(f"user:{username}", None)


async def _get_user_overrides(db: AsyncSession, username: str) -> UserLimitOverrides:
    cached, ok = _cache_get(f"user:{username}")
    if ok:
        cached_overrides: UserLimitOverrides = cached
        return cached_overrides
    user = await db.scalar(select(User).where(User.username == username))
    # Read back as stored, unvalidated: a value set by hand in SQL must not 500 the hot
    # path (the limiter itself handles a non-positive rate).
    overrides = (
        UserLimitOverrides.model_construct(
            **{field: getattr(user, field) for field in UserLimitOverrides.model_fields}
        )
        if user
        else UserLimitOverrides()
    )
    _cache_put(f"user:{username}", overrides)
    return overrides


async def effective_limits(db: AsyncSession, username: str, limit_class: LimitClass) -> RateLimit:
    config = await get_runtime_config(db)
    overrides = await _get_user_overrides(db, username)
    return overrides.apply(limit_class, config.rate_limits[limit_class])


@dataclass(frozen=True)
class DomainLimits:
    """A domain's own limits; None = the global value."""

    max_upload_bytes: int | None = None
    running_timeout_seconds: int | None = None


async def get_domain_limits(db: AsyncSession) -> dict[str, DomainLimits]:
    """Every domain that overrides a limit, cached like the config values (the table is
    small, and the submit path and the reaper both need it)."""
    cached, ok = _cache_get("domain_limits")
    if ok:
        limits: dict[str, DomainLimits] = cached
        return limits
    rows = await db.execute(
        select(Domain.name, Domain.max_upload_bytes, Domain.running_timeout_seconds).where(
            (Domain.max_upload_bytes.is_not(None)) | (Domain.running_timeout_seconds.is_not(None))
        )
    )
    limits = {name: DomainLimits(upload, timeout) for name, upload, timeout in rows.all()}
    _cache_put("domain_limits", limits)
    return limits


async def get_state_ttl_seconds(db: AsyncSession) -> int:
    """Redis job-state TTL: long enough to outlive the full queued+running lifecycle.

    Computed (not configured) so it can never be set below the running timeout, which
    would let a running job's Redis state expire before it can be harvested or reaped.
    """
    config = await get_runtime_config(db)
    running = config.running_timeout_seconds
    # A domain with a longer running timeout (maps) must not outlive its Redis state.
    for limits in (await get_domain_limits(db)).values():
        if limits.running_timeout_seconds is not None:
            running = max(running, limits.running_timeout_seconds)
    return config.queued_timeout_seconds + running + STATE_TTL_MARGIN_SECONDS
