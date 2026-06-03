import time
from functools import lru_cache

import redis.asyncio as aioredis
from fastapi import Depends, HTTPException, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.models.db import get_db
from app.models.user import User
from app.services.redis_jobs import check_rate_limit

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_master_key_header = APIKeyHeader(name="X-Master-Key", auto_error=False)

# Simple TTL cache for user lookups
_user_cache: dict[str, tuple[str, float]] = {}
_USER_CACHE_TTL = 10.0


@lru_cache
def get_settings() -> Settings:
    return Settings()


async def get_redis(
    settings: Settings = Depends(get_settings),
) -> aioredis.Redis:
    return aioredis.from_url(settings.redis_url, decode_responses=False)


async def require_user(
    request: Request,
    api_key: str | None = Security(_api_key_header),
    db: AsyncSession = Depends(get_db),
) -> str:
    if not api_key:
        raise HTTPException(status_code=401, detail="Missing X-API-Key header")

    from app.services.auth import hash_key

    hashed = hash_key(api_key)

    # Check cache
    now = time.time()
    if hashed in _user_cache:
        username, cached_at = _user_cache[hashed]
        if now - cached_at < _USER_CACHE_TTL:
            return username

    result = await db.execute(
        select(User).where(User.hashed_key == hashed, User.active == True)  # noqa: E712
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid API key")

    _user_cache[hashed] = (user.username, now)
    return user.username


async def require_master(
    request: Request,
    key: str | None = Security(_master_key_header),
    settings: Settings = Depends(get_settings),
) -> None:
    if not key or key != settings.master_key:
        raise HTTPException(status_code=403, detail="Invalid master key")


def rate_limit_submit():
    async def _check(
        request: Request,
        username: str = Depends(require_user),
        r: aioredis.Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings),
    ) -> str:
        allowed = await check_rate_limit(
            r, f"rl:submit:{username}", settings.rate_limit_submit_per_minute
        )
        if not allowed:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        return username

    return _check


def rate_limit_query():
    async def _check(
        request: Request,
        username: str = Depends(require_user),
        r: aioredis.Redis = Depends(get_redis),
        settings: Settings = Depends(get_settings),
    ) -> str:
        allowed = await check_rate_limit(
            r, f"rl:query:{username}", settings.rate_limit_query_per_minute
        )
        if not allowed:
            raise HTTPException(status_code=429, detail="Rate limit exceeded")
        return username

    return _check


def rate_limit_ws():
    async def _check(
        username: str,
        r: aioredis.Redis,
        settings: Settings = Depends(get_settings),
    ) -> bool:
        allowed = await check_rate_limit(
            r, f"rl:ws:{username}", settings.rate_limit_ws_connects_per_minute
        )
        return allowed

    return _check
