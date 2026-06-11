from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deps import get_settings, require_master
from app.models.backend import Backend
from app.models.db import get_db
from app.models.user import User
from app.schemas.backend import BackendCreate, BackendResponse, BackendUpdate
from app.schemas.user import (
    EffectiveLimits,
    SetKeyRequest,
    UserCreate,
    UserLimitOverrides,
    UserLimitsResponse,
    UserList,
    UserResponse,
)
from app.services import config as config_service
from app.services.auth import (
    encrypt_backend_key,
    generate_key,
    hash_key,
)

router = APIRouter(dependencies=[Depends(require_master)])


# --- Users ---


@router.get(
    "/users",
    response_model=list[UserList],
    summary="List all users",
    responses={401: {"description": "Missing or invalid master key"}},
)
async def list_users(db: AsyncSession = Depends(get_db)):
    """List all users with their status and per-user rate-limit overrides.

    Requires a valid master key. Newest users are returned first.
    """
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return [
        UserList(
            username=u.username,
            active=u.active,
            created_at=u.created_at,
            rate_submit_per_minute=u.rate_submit_per_minute,
            burst_submit=u.burst_submit,
            rate_query_per_minute=u.rate_query_per_minute,
            burst_query=u.burst_query,
            rate_ws_per_minute=u.rate_ws_per_minute,
            burst_ws=u.burst_ws,
        )
        for u in result.scalars().all()
    ]


@router.post(
    "/users",
    response_model=UserResponse,
    status_code=201,
    summary="Create a user",
    responses={
        409: {"description": "Username already exists"},
        401: {"description": "Missing or invalid master key"},
    },
)
async def create_user(body: UserCreate, db: AsyncSession = Depends(get_db)):
    """Create a new user and generate their API key.

    Requires a valid master key. The raw API key is returned once in the response
    and is never stored or retrievable again (only its hash is persisted).
    """
    existing = await db.execute(select(User).where(User.username == body.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username already exists")

    raw_key, hashed = generate_key()
    user = User(username=body.username, hashed_key=hashed)
    db.add(user)
    await db.commit()
    return UserResponse(username=body.username, api_key=raw_key)


@router.delete(
    "/users/{username}",
    summary="Deactivate a user",
    responses={
        404: {"description": "User not found"},
        401: {"description": "Missing or invalid master key"},
    },
)
async def deactivate_user(username: str, db: AsyncSession = Depends(get_db)):
    """Soft-disable a user by setting ``active=False``.

    Requires a valid master key. The user and their key hash are retained, so the
    account can later be re-enabled; this does not delete the user.
    """
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await db.execute(update(User).where(User.username == username).values(active=False))
    await db.commit()
    return {"status": "deactivated"}


@router.post(
    "/users/{username}/enable",
    summary="Enable a user",
    responses={
        404: {"description": "User not found"},
        401: {"description": "Missing or invalid master key"},
    },
)
async def enable_user(username: str, db: AsyncSession = Depends(get_db)):
    """Re-activate a previously deactivated user by setting ``active=True``.

    Requires a valid master key.
    """
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await db.execute(update(User).where(User.username == username).values(active=True))
    await db.commit()
    return {"status": "enabled"}


@router.post(
    "/users/{username}/rotate-key",
    response_model=UserResponse,
    summary="Rotate a user's API key",
    responses={
        404: {"description": "User not found"},
        401: {"description": "Missing or invalid master key"},
    },
)
async def rotate_key(username: str, db: AsyncSession = Depends(get_db)):
    """Generate a fresh API key for the user, invalidating the previous one.

    Requires a valid master key. The new raw API key is returned once in the
    response and is never retrievable again (only its hash is persisted).
    """
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    raw_key, hashed = generate_key()
    await db.execute(update(User).where(User.username == username).values(hashed_key=hashed))
    await db.commit()
    return UserResponse(username=username, api_key=raw_key)


@router.put(
    "/users/{username}/key",
    summary="Set a user's API key",
    responses={
        404: {"description": "User not found"},
        401: {"description": "Missing or invalid master key"},
    },
)
async def set_key(username: str, body: SetKeyRequest, db: AsyncSession = Depends(get_db)):
    """Replace the user's API key with a caller-supplied value.

    Requires a valid master key. The provided key is hashed before storage,
    invalidating any previous key for this user.
    """
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    hashed = hash_key(body.key)
    await db.execute(update(User).where(User.username == username).values(hashed_key=hashed))
    await db.commit()
    return {"status": "key updated"}


@router.patch(
    "/users/{username}",
    response_model=UserLimitsResponse,
    summary="Update rate-limit overrides",
    responses={
        404: {"description": "User not found"},
        401: {"description": "Missing or invalid master key"},
    },
)
async def update_user_limits(
    username: str,
    body: UserLimitOverrides,
    db: AsyncSession = Depends(get_db),
):
    """Update per-user rate-limit overrides and return the resolved effective limits.

    Requires a valid master key. Only fields present in the request are changed;
    an explicit null clears an override so that class inherits the global default.
    Cached limits for the user are invalidated so changes take effect immediately.
    """
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # exclude_unset: only fields present in the request change;
    # an explicit null clears the override back to inherit.
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    await db.commit()
    await db.refresh(user)
    config_service.invalidate_user(username)

    # Resolve each class and flatten back into the override column names so the
    # effective view mirrors the overrides shape (every field non-null).
    resolved: dict[str, int] = {}
    for cls, (per_col, burst_col) in config_service.USER_OVERRIDE_COLUMNS.items():
        limits = await config_service.effective_limits(db, username, cls)
        resolved[per_col] = limits.per_minute
        resolved[burst_col] = limits.burst

    return UserLimitsResponse(
        username=username,
        overrides=UserLimitOverrides(
            rate_submit_per_minute=user.rate_submit_per_minute,
            burst_submit=user.burst_submit,
            rate_query_per_minute=user.rate_query_per_minute,
            burst_query=user.burst_query,
            rate_ws_per_minute=user.rate_ws_per_minute,
            burst_ws=user.burst_ws,
        ),
        effective=EffectiveLimits(**resolved),
    )


# --- Backends ---


@router.get(
    "/backends",
    response_model=list[BackendResponse],
    summary="List all backends",
    responses={401: {"description": "Missing or invalid master key"}},
)
async def list_backends(db: AsyncSession = Depends(get_db)):
    """List all configured OCR backends ordered by id.

    Requires a valid master key. Stored backend API keys are never returned.
    """
    result = await db.execute(select(Backend).order_by(Backend.id))
    return [
        BackendResponse(
            id=b.id,
            url=b.url,
            label=b.label,
            enabled=b.enabled,
            max_inflight=b.max_inflight,
            created_at=b.created_at,
        )
        for b in result.scalars().all()
    ]


@router.post(
    "/backends",
    response_model=BackendResponse,
    status_code=201,
    summary="Create a backend",
    responses={401: {"description": "Missing or invalid master key"}},
)
async def create_backend(
    body: BackendCreate,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Register a new OCR backend.

    Requires a valid master key. If an API key is supplied it is encrypted at rest;
    the key is never echoed back in the response.
    """
    api_key_enc = None
    if body.api_key:
        api_key_enc = encrypt_backend_key(body.api_key, settings.key_encryption_secret)

    backend = Backend(
        url=body.url,
        label=body.label,
        api_key_enc=api_key_enc,
        max_inflight=body.max_inflight,
    )
    db.add(backend)
    await db.commit()
    await db.refresh(backend)
    return BackendResponse(
        id=backend.id,
        url=backend.url,
        label=backend.label,
        enabled=backend.enabled,
        max_inflight=backend.max_inflight,
        created_at=backend.created_at,
    )


@router.patch(
    "/backends/{backend_id}",
    response_model=BackendResponse,
    summary="Update a backend",
    responses={
        404: {"description": "Backend not found"},
        401: {"description": "Missing or invalid master key"},
    },
)
async def update_backend(
    backend_id: int,
    body: BackendUpdate,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Update fields of an existing backend.

    Requires a valid master key. Only fields present in the request are changed;
    a supplied API key is encrypted at rest and never echoed back in the response.
    """
    result = await db.execute(select(Backend).where(Backend.id == backend_id))
    backend = result.scalar_one_or_none()
    if not backend:
        raise HTTPException(status_code=404, detail="Backend not found")

    update_data = body.model_dump(exclude_unset=True)
    if "api_key" in update_data:
        api_key = update_data.pop("api_key")
        if api_key:
            update_data["api_key_enc"] = encrypt_backend_key(
                api_key, settings.key_encryption_secret
            )

    for field, value in update_data.items():
        setattr(backend, field, value)

    await db.commit()
    await db.refresh(backend)
    return BackendResponse(
        id=backend.id,
        url=backend.url,
        label=backend.label,
        enabled=backend.enabled,
        max_inflight=backend.max_inflight,
        created_at=backend.created_at,
    )


# --- Config ---


@router.get(
    "/config",
    summary="Get runtime config",
    responses={401: {"description": "Missing or invalid master key"}},
)
async def get_config(db: AsyncSession = Depends(get_db)):
    """Return all runtime configuration values as a key/value map.

    Requires a valid master key.
    """
    return await config_service.get_all(db)


@router.put(
    "/config",
    summary="Update runtime config",
    responses={
        400: {"description": "Empty config payload"},
        401: {"description": "Missing or invalid master key"},
    },
)
async def update_config(values: dict[str, Any], db: AsyncSession = Depends(get_db)):
    """Upsert one or more runtime configuration values from the request body.

    Requires a valid master key. An empty payload is rejected.
    """
    if not values:
        raise HTTPException(status_code=400, detail="Empty config payload")
    await config_service.set_values(db, values)
    return {"status": "updated"}
