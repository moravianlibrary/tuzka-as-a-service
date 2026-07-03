import hmac
import math
import time
from collections.abc import Awaitable, Callable
from functools import lru_cache

import redis.asyncio as aioredis
from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.exceptions import RateLimited, Unauthorized
from app.models.db import get_db
from app.models.user import User
from app.services import config as config_service
from app.services import rate_limit
from app.services.auth import hash_key

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_master_key_header = APIKeyHeader(name="X-Master-Key", auto_error=False)

# TTL cache for successful user lookups, keyed by hashed API key. Only *valid* users
# are cached (invalid keys raise before reaching the insert), so it is bounded by the
# active-user count; the hard cap is belt-and-suspenders against unbounded growth.
_user_cache: dict[str, tuple[str, float]] = {}
_USER_CACHE_TTL = 10.0
_USER_CACHE_MAX = 10_000


@lru_cache
def get_settings() -> Settings:
    return Settings()


async def get_redis(request: Request) -> aioredis.Redis:
    # The pooled client is created once in the app lifespan; hand out that shared
    # instance rather than opening a new connection pool per request.
    return request.app.state.redis  # type: ignore[no-any-return]


async def authenticate_api_key(db: AsyncSession, api_key: str) -> str | None:
    """Return the username for a valid, active API key, or None (no raise).

    The single authn path shared by the HTTP dependency and the WebSocket handler.
    Cached with a short TTL; ``invalidate_auth_cache`` drops it when a user's key or
    active state changes so a revoked key stops working at once.
    """
    hashed = hash_key(api_key)

    now = time.time()
    entry = _user_cache.get(hashed)
    if entry is not None and now - entry[1] < _USER_CACHE_TTL:
        return entry[0]

    user = await db.scalar(select(User).where(User.hashed_key == hashed, User.active.is_(True)))
    if user is None:
        return None

    if len(_user_cache) >= _USER_CACHE_MAX:
        _user_cache.pop(next(iter(_user_cache)))  # evict oldest (insertion order)
    _user_cache[hashed] = (user.username, now)
    return user.username


def invalidate_auth_cache() -> None:
    """Drop all cached API-key lookups. Call after any change to a user's key or active
    state (create/delete/rotate/disable) so a revoked or rotated key can't keep
    authenticating for the cache TTL."""
    _user_cache.clear()


async def require_user(
    request: Request,
    api_key: str | None = Security(_api_key_header),
    db: AsyncSession = Depends(get_db),
) -> str:
    if not api_key:
        raise Unauthorized("Missing X-API-Key header")

    username = await authenticate_api_key(db, api_key)
    if username is None:
        raise Unauthorized("Invalid API key")

    return username


async def require_master(
    request: Request,
    key: str | None = Security(_master_key_header),
    settings: Settings = Depends(get_settings),
) -> None:
    from app.services import dash_session

    if key and hmac.compare_digest(key, settings.master_key):
        return
    cookie = request.cookies.get(dash_session.COOKIE_NAME)
    if cookie and dash_session.verify(settings.master_key, cookie):
        return
    raise Unauthorized("Invalid master key")


def _rate_limit_dep(limit_class: str) -> Callable[..., Awaitable[str]]:
    async def _check(
        request: Request,
        username: str = Depends(require_user),
        r: aioredis.Redis = Depends(get_redis),
        db: AsyncSession = Depends(get_db),
    ) -> str:
        limits = await config_service.effective_limits(db, username, limit_class)
        result = await rate_limit.check(r, limit_class, username, limits.per_minute, limits.burst)
        if not result.allowed:
            raise RateLimited(retry_after=math.ceil(result.retry_after))
        return username

    return _check


def rate_limit_submit() -> Callable[..., Awaitable[str]]:
    return _rate_limit_dep("submit")


def rate_limit_query() -> Callable[..., Awaitable[str]]:
    return _rate_limit_dep("query")
