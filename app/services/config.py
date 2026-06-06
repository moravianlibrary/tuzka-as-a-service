import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.config import ConfigEntry
from app.models.user import User

# Short in-process cache: hot path must not hit Postgres per request,
# admin changes still take effect within seconds on every replica.
CACHE_TTL_SECONDS = 10.0
_cache: dict[str, tuple[Any, float]] = {}

STORAGE_TTL_DEFAULT_MINUTES = 60


@dataclass(frozen=True)
class ClassLimits:
    per_minute: int
    burst: int


FALLBACK_LIMITS: dict[str, ClassLimits] = {
    "submit": ClassLimits(per_minute=60, burst=10),
    "query": ClassLimits(per_minute=120, burst=20),
    "ws_connect": ClassLimits(per_minute=5, burst=2),
}

# limit class -> (users.<per-minute column>, users.<burst column>)
USER_OVERRIDE_COLUMNS: dict[str, tuple[str, str]] = {
    "submit": ("rate_submit_per_minute", "burst_submit"),
    "query": ("rate_query_per_minute", "burst_query"),
    "ws_connect": ("rate_ws_per_minute", "burst_ws"),
}


def resolve(
    per_minute_override: int | None,
    burst_override: int | None,
    default: ClassLimits,
) -> ClassLimits:
    return ClassLimits(
        per_minute=per_minute_override if per_minute_override is not None else default.per_minute,
        burst=burst_override if burst_override is not None else default.burst,
    )


def parse_default_limits(value: Any, fallback: ClassLimits) -> ClassLimits:
    if not isinstance(value, dict):
        return fallback
    return ClassLimits(
        per_minute=int(value.get("per_minute", fallback.per_minute)),
        burst=int(value.get("burst", fallback.burst)),
    )


def _cache_get(key: str) -> tuple[Any, bool]:
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
    for key, value in values.items():
        result = await db.execute(select(ConfigEntry).where(ConfigEntry.key == key))
        entry = result.scalar_one_or_none()
        if entry:
            entry.value = value
        else:
            db.add(ConfigEntry(key=key, value=value))
    await db.commit()
    for key in values:
        _cache.pop(f"cfg:{key}", None)


def invalidate_user(username: str) -> None:
    _cache.pop(f"user:{username}", None)


async def get_default_limits(db: AsyncSession, limit_class: str) -> ClassLimits:
    value = await get_value(db, f"rate_limit.{limit_class}")
    return parse_default_limits(value, FALLBACK_LIMITS[limit_class])


async def _get_user_overrides(db: AsyncSession, username: str) -> dict[str, int | None]:
    overrides, ok = _cache_get(f"user:{username}")
    if ok:
        return overrides
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    columns = [c for pair in USER_OVERRIDE_COLUMNS.values() for c in pair]
    overrides = {c: getattr(user, c) if user else None for c in columns}
    _cache_put(f"user:{username}", overrides)
    return overrides


async def effective_limits(db: AsyncSession, username: str, limit_class: str) -> ClassLimits:
    default = await get_default_limits(db, limit_class)
    overrides = await _get_user_overrides(db, username)
    per_col, burst_col = USER_OVERRIDE_COLUMNS[limit_class]
    return resolve(overrides[per_col], overrides[burst_col], default)


async def get_storage_ttl_minutes(db: AsyncSession, buckets: list[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for bucket in buckets:
        value = await get_value(db, f"storage.{bucket}_ttl_minutes")
        out[bucket] = int(value) if value is not None else STORAGE_TTL_DEFAULT_MINUTES
    return out
