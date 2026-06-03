from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings
from app.deps import get_settings, require_master
from app.models.backend import Backend
from app.models.db import get_db
from app.models.storage_config import StorageConfig
from app.models.user import User
from app.schemas.backend import BackendCreate, BackendResponse, BackendUpdate
from app.schemas.user import SetKeyRequest, UserCreate, UserList, UserResponse
from app.services.auth import (
    encrypt_backend_key,
    generate_key,
    hash_key,
)

router = APIRouter(dependencies=[Depends(require_master)])


# --- Users ---


@router.get("/users", response_model=list[UserList])
async def list_users(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).order_by(User.created_at))
    return [
        UserList(username=u.username, active=u.active, created_at=u.created_at)
        for u in result.scalars().all()
    ]


@router.post("/users", response_model=UserResponse, status_code=201)
async def create_user(body: UserCreate, db: AsyncSession = Depends(get_db)):
    existing = await db.execute(select(User).where(User.username == body.username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Username already exists")

    raw_key, hashed = generate_key()
    user = User(username=body.username, hashed_key=hashed)
    db.add(user)
    await db.commit()
    return UserResponse(username=body.username, api_key=raw_key)


@router.delete("/users/{username}")
async def deactivate_user(username: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await db.execute(update(User).where(User.username == username).values(active=False))
    await db.commit()
    return {"status": "deactivated"}


@router.post("/users/{username}/rotate-key", response_model=UserResponse)
async def rotate_key(username: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    raw_key, hashed = generate_key()
    await db.execute(update(User).where(User.username == username).values(hashed_key=hashed))
    await db.commit()
    return UserResponse(username=username, api_key=raw_key)


@router.put("/users/{username}/key")
async def set_key(username: str, body: SetKeyRequest, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    hashed = hash_key(body.key)
    await db.execute(update(User).where(User.username == username).values(hashed_key=hashed))
    await db.commit()
    return {"status": "key updated"}


# --- Backends ---


@router.get("/backends", response_model=list[BackendResponse])
async def list_backends(db: AsyncSession = Depends(get_db)):
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


@router.post("/backends", response_model=BackendResponse, status_code=201)
async def create_backend(
    body: BackendCreate,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
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


@router.patch("/backends/{backend_id}", response_model=BackendResponse)
async def update_backend(
    backend_id: int,
    body: BackendUpdate,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
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


@router.delete("/backends/{backend_id}")
async def delete_backend(backend_id: int, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Backend).where(Backend.id == backend_id))
    backend = result.scalar_one_or_none()
    if not backend:
        raise HTTPException(status_code=404, detail="Backend not found")
    await db.delete(backend)
    await db.commit()
    return {"status": "deleted"}


# --- Storage Config ---


@router.get("/storage-config")
async def get_storage_config(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(StorageConfig))
    return [{"bucket": sc.bucket, "ttl_minutes": sc.ttl_minutes} for sc in result.scalars().all()]


@router.put("/storage-config")
async def update_storage_config(configs: list[dict], db: AsyncSession = Depends(get_db)):
    for cfg in configs:
        result = await db.execute(
            select(StorageConfig).where(StorageConfig.bucket == cfg["bucket"])
        )
        sc = result.scalar_one_or_none()
        if sc:
            sc.ttl_minutes = cfg["ttl_minutes"]
        else:
            db.add(StorageConfig(bucket=cfg["bucket"], ttl_minutes=cfg["ttl_minutes"]))
    await db.commit()
    return {"status": "updated"}
