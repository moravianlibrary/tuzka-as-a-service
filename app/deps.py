import hmac
import math
import time
from collections.abc import Awaitable, Callable
from functools import lru_cache

import redis.asyncio as aioredis
from fastapi import Depends, Request, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.exceptions import NotFound, RateLimited, Unauthorized
from app.models.db import get_db
from app.models.user import User
from app.services import config as config_service
from app.services import rate_limit
from app.services.auth import hash_key
from app.services.scopes import Principal
from app.vocabulary import LimitClass

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
_load_bearer = HTTPBearer(auto_error=False)

# TTL cache for successful user lookups, keyed by hashed API key. Only *valid* users
# are cached (invalid keys raise before reaching the insert), so it is bounded by the
# active-user count; the hard cap is belt-and-suspenders against unbounded growth.
_user_cache: dict[str, tuple[Principal, float]] = {}
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
    """Return the username for a valid, active API key, or None (no raise)."""
    principal = await authenticate_principal(db, api_key)
    return principal.username if principal is not None else None


async def authenticate_principal(db: AsyncSession, api_key: str) -> Principal | None:
    """Return the caller (username + key scope) for a valid, active API key, or None.

    The single authn path shared by the HTTP dependencies and the WebSocket handler.
    Cached with a short TTL, so a key, scope or active-state change made with
    taas-admin reaches every API replica within it.
    """
    hashed = hash_key(api_key)

    now = time.time()
    entry = _user_cache.get(hashed)
    if entry is not None and now - entry[1] < _USER_CACHE_TTL:
        return entry[0]

    user = await db.scalar(select(User).where(User.hashed_key == hashed, User.active.is_(True)))
    if user is None:
        return None

    principal = Principal(
        username=user.username,
        allowed_domains=_scope(user.allowed_domains),
        allowed_outputs=_scope(user.allowed_outputs),
    )
    if len(_user_cache) >= _USER_CACHE_MAX:
        _user_cache.pop(next(iter(_user_cache)))  # evict oldest (insertion order)
    _user_cache[hashed] = (principal, now)
    return principal


def _scope(values: list[str] | None) -> frozenset[str] | None:
    return frozenset(values) if values is not None else None


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


async def require_principal(
    api_key: str | None = Security(_api_key_header),
    db: AsyncSession = Depends(get_db),
) -> Principal:
    """The caller with their key's scope, for handlers that enforce it (cached, so
    stacking it with a rate-limit dependency costs no extra query)."""
    if not api_key:
        raise Unauthorized("Missing X-API-Key header")
    principal = await authenticate_principal(db, api_key)
    if principal is None:
        raise Unauthorized("Invalid API key")
    return principal


async def require_load_token(
    credentials: HTTPAuthorizationCredentials | None = Security(_load_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    """Guard for GET /load. The ingress exposes the whole API, so this token is what
    keeps the metric internal; with no LOAD_TOKEN configured the endpoint doesn't exist."""
    if not settings.load_token:
        raise NotFound()
    if credentials is None or not hmac.compare_digest(credentials.credentials, settings.load_token):
        raise Unauthorized("Invalid load token")


def _rate_limit_dep(limit_class: LimitClass) -> Callable[..., Awaitable[str]]:
    async def _check(
        request: Request,
        username: str = Depends(require_user),
        r: aioredis.Redis = Depends(get_redis),
        db: AsyncSession = Depends(get_db),
    ) -> str:
        limit = await config_service.effective_limits(db, username, limit_class)
        result = await rate_limit.check(r, limit_class, username, limit)
        if not result.allowed:
            raise RateLimited(retry_after=math.ceil(result.retry_after))
        return username

    return _check


def rate_limit_submit() -> Callable[..., Awaitable[str]]:
    return _rate_limit_dep(LimitClass.SUBMIT)


def rate_limit_query() -> Callable[..., Awaitable[str]]:
    return _rate_limit_dep(LimitClass.QUERY)
