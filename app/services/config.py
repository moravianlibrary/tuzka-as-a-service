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
from app.schemas.runtime_config import FALLBACK_RATE_LIMITS, RateLimit
from app.vocabulary import LimitClass

# Short in-process cache: hot path must not hit Postgres per request,
# admin changes still take effect within seconds on every replica.
# The user: key space is bounded because callers only pass authenticated
# usernames (require_user gates the hot path), so this dict cannot grow
# unboundedly from unauthenticated traffic.
# TODO(rrandiak): we should make pydantic model from this
CACHE_TTL_SECONDS = 10.0
_cache: dict[str, tuple[Any, float]] = {}

STORAGE_TTL_DEFAULT_MINUTES = 60
JOB_QUEUED_TIMEOUT_DEFAULT_SECONDS = 900
JOB_RUNNING_TIMEOUT_DEFAULT_SECONDS = 300
JOB_MAX_REQUEUES_DEFAULT = 5
PRESIGNED_TTL_DEFAULT_MINUTES = 60
# Retention (step 6): finished job rows, and result data.
JOB_DONE_TTL_DEFAULT_SECONDS = 86_400
JOB_FAILED_TTL_DEFAULT_SECONDS = 604_800
RESULTS_MAX_AGE_DEFAULT_SECONDS = 86_400
# Dispatch: a backend already running a batch's pages wins its next page when it would
# finish within this fraction of the fastest one (step 8, D7).
AFFINITY_TOLERANCE_DEFAULT = 0.15
STATE_TTL_MARGIN_SECONDS = 60
# Seconds per page /load assumes for a domain with no measured pages yet.
LOAD_DEFAULT_SECONDS_PER_PAGE = 10.0


def _cache_get(key: str) -> tuple[Any, bool]:
    # ok=True with value=None means a cached negative lookup (e.g. key not in DB),
    # which is distinct from a cache miss (ok=False).
    hit = _cache.get(key)
    if hit is not None and time.monotonic() - hit[1] < CACHE_TTL_SECONDS:
        return hit[0], True
    return None, False


def _cache_put(key: str, value: Any) -> None:
    _cache[key] = (value, time.monotonic())


async def get_value(db: AsyncSession, key: str) -> Any | None:
    value, ok = _cache_get(f"cfg:{key}")
    if ok:
        return value
    result = await db.execute(select(ConfigEntry).where(ConfigEntry.key == key))
    entry = result.scalar_one_or_none()
    value = entry.value if entry else None
    _cache_put(f"cfg:{key}", value)
    return value


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
    for key in values:
        _cache.pop(f"cfg:{key}", None)


def invalidate_user(username: str) -> None:
    _cache.pop(f"user:{username}", None)


async def get_default_limits(db: AsyncSession, limit_class: LimitClass) -> RateLimit:
    value = await get_value(db, f"rate_limit.{limit_class}")
    return RateLimit.from_config(value, FALLBACK_RATE_LIMITS[limit_class])


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
    default = await get_default_limits(db, limit_class)
    overrides = await _get_user_overrides(db, username)
    return overrides.apply(limit_class, default)


async def get_storage_ttl_minutes(db: AsyncSession, buckets: list[str]) -> dict[str, int]:
    keys = [f"storage.{bucket}_ttl_minutes" for bucket in buckets]
    result = await db.execute(select(ConfigEntry).where(ConfigEntry.key.in_(keys)))
    values = {entry.key: entry.value for entry in result.scalars().all()}
    out: dict[str, int] = {}
    for bucket in buckets:
        value = values.get(f"storage.{bucket}_ttl_minutes")
        out[bucket] = int(value) if value is not None else STORAGE_TTL_DEFAULT_MINUTES
    return out


async def get_job_queued_timeout_seconds(db: AsyncSession) -> int:
    value = await get_value(db, "jobs.queued_timeout_seconds")
    return int(value) if value is not None else JOB_QUEUED_TIMEOUT_DEFAULT_SECONDS


async def get_job_running_timeout_seconds(db: AsyncSession) -> int:
    value = await get_value(db, "jobs.running_timeout_seconds")
    return int(value) if value is not None else JOB_RUNNING_TIMEOUT_DEFAULT_SECONDS


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


async def get_presigned_ttl_minutes(db: AsyncSession) -> int:
    value = await get_value(db, "presigned.ttl_minutes")
    return int(value) if value is not None else PRESIGNED_TTL_DEFAULT_MINUTES


async def get_done_ttl_seconds(db: AsyncSession) -> int:
    value = await get_value(db, "jobs.done_ttl_seconds")
    return int(value) if value is not None else JOB_DONE_TTL_DEFAULT_SECONDS


async def get_failed_ttl_seconds(db: AsyncSession) -> int:
    value = await get_value(db, "jobs.failed_ttl_seconds")
    return int(value) if value is not None else JOB_FAILED_TTL_DEFAULT_SECONDS


async def get_results_max_age_seconds(db: AsyncSession) -> int:
    value = await get_value(db, "storage.results_max_age_seconds")
    return int(value) if value is not None else RESULTS_MAX_AGE_DEFAULT_SECONDS


async def get_results_max_bytes(db: AsyncSession) -> int | None:
    """The results storage cap; None (unset) = no cap, results go by age only."""
    value = await get_value(db, "storage.results_max_bytes")
    return int(value) if value is not None else None


async def get_affinity_tolerance(db: AsyncSession) -> float:
    value = await get_value(db, "dispatch.affinity_tolerance")
    return float(value) if value is not None else AFFINITY_TOLERANCE_DEFAULT


async def get_max_requeues(db: AsyncSession) -> int:
    value = await get_value(db, "jobs.max_requeues")
    return int(value) if value is not None else JOB_MAX_REQUEUES_DEFAULT


async def get_default_seconds_per_page(db: AsyncSession) -> float:
    value = await get_value(db, "load.default_seconds_per_page")
    return float(value) if value is not None else LOAD_DEFAULT_SECONDS_PER_PAGE


async def get_state_ttl_seconds(db: AsyncSession) -> int:
    """Redis job-state TTL: long enough to outlive the full queued+running lifecycle.

    Computed (not configured) so it can never be set below the running timeout, which
    would let a running job's Redis state expire before it can be harvested or reaped.
    """
    queued = await get_job_queued_timeout_seconds(db)
    running = await get_job_running_timeout_seconds(db)
    # A domain with a longer running timeout (maps) must not outlive its Redis state.
    for limits in (await get_domain_limits(db)).values():
        if limits.running_timeout_seconds is not None:
            running = max(running, limits.running_timeout_seconds)
    return queued + running + STATE_TTL_MARGIN_SECONDS
