"""The admin operations: users, backends, domains, runtime config, failed jobs.

Plain async functions over an ``AsyncSession``, called by the ``taas-admin`` CLI. They
validate through ``app.schemas.api``, raise the domain errors from ``app.exceptions``
(``NotFound``, ``Conflict``), and commit their own writes.

API processes cache key lookups for 10 s (``app.deps``), so a key or active-state
change made here reaches every API replica within that window.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.exceptions import BadRequest, Conflict, NotFound
from app.models.backend import Backend
from app.models.domain import Domain
from app.models.job import Job
from app.models.user import User
from app.schemas.api.backend import BackendCreate, BackendUpdate
from app.schemas.api.user import (
    EffectiveLimits,
    UserLimitOverrides,
    UserLimitsResponse,
    UserUpdate,
)
from app.services import config as config_service
from app.services.auth import encrypt_backend_key, generate_key, hash_key
from app.services.domain_sync import apply_declared_capabilities
from app.vocabulary import JobStatus, LimitClass

# --- Users ---


async def list_users(db: AsyncSession) -> list[User]:
    """Every user, newest first."""
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    return list(result.scalars().all())


async def _user(db: AsyncSession, username: str) -> User:
    user = await db.scalar(select(User).where(User.username == username))
    if user is None:
        raise NotFound(f"User {username!r} not found")
    return user


async def create_user(db: AsyncSession, username: str) -> str:
    """Create a user and return their API key. Only its hash is stored, so this is the
    one time the raw key exists."""
    if await db.scalar(select(User).where(User.username == username)):
        raise Conflict(f"User {username!r} already exists")
    raw_key, hashed = generate_key()
    db.add(User(username=username, hashed_key=hashed))
    await db.commit()
    return raw_key


async def delete_user(db: AsyncSession, username: str) -> None:
    """Delete a user. Refused while jobs still reference them (they hold a foreign key);
    once the job TTLs have removed those, it goes through. To stop a user without
    deleting, disable them instead."""
    user = await _user(db, username)
    count_jobs = select(func.count()).select_from(Job).where(Job.username == username)
    job_count = await db.scalar(count_jobs)
    if job_count:
        raise Conflict(f"User {username!r} still has {job_count} job(s); cannot delete")
    await db.delete(user)
    await db.commit()


async def rotate_key(db: AsyncSession, username: str) -> str:
    """Give the user a fresh API key (returned once); the previous one stops working."""
    await _user(db, username)
    raw_key, hashed = generate_key()
    await db.execute(update(User).where(User.username == username).values(hashed_key=hashed))
    await db.commit()
    return raw_key


async def set_key(db: AsyncSession, username: str, key: str) -> None:
    """Replace the user's API key with a caller-chosen one (stored hashed)."""
    await _user(db, username)
    hashed = hash_key(key)
    await db.execute(update(User).where(User.username == username).values(hashed_key=hashed))
    await db.commit()


async def update_user(db: AsyncSession, username: str, body: UserUpdate) -> UserLimitsResponse:
    """Apply the fields present in ``body`` (rate-limit overrides, active, priority; an
    explicit null clears an override) and return the user's effective limits."""
    user = await _user(db, username)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(user, field, value)
    await db.commit()
    await db.refresh(user)
    config_service.invalidate_user(username)

    effective = {
        cls: await config_service.effective_limits(db, username, cls) for cls in LimitClass
    }
    return UserLimitsResponse(
        username=username,
        overrides=UserLimitOverrides.model_validate(user, from_attributes=True),
        effective=EffectiveLimits.from_limits(effective),
    )


async def set_scopes(
    db: AsyncSession,
    username: str,
    domains: list[str] | None,
    outputs: list[str] | None,
) -> User:
    """Limit the user's key to these domains / outputs; None lifts that limit."""
    user = await _user(db, username)
    user.allowed_domains = sorted(set(domains)) if domains is not None else None
    user.allowed_outputs = sorted(set(outputs)) if outputs is not None else None
    await db.commit()
    await db.refresh(user)
    return user


# --- Backends ---


async def _commit_backend(db: AsyncSession) -> None:
    """Commit a backend write, turning the unique-URL violation into a Conflict."""
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise Conflict("A backend with this URL already exists") from e


async def _backend(db: AsyncSession, backend_id: int) -> Backend:
    backend = await db.scalar(select(Backend).where(Backend.id == backend_id))
    if backend is None:
        raise NotFound(f"Backend {backend_id} not found")
    return backend


async def list_backends(db: AsyncSession) -> list[Backend]:
    result = await db.execute(select(Backend).order_by(Backend.id))
    return list(result.scalars().all())


async def create_backend(db: AsyncSession, body: BackendCreate, secret: str) -> Backend:
    """Register a new backend; its API key is stored encrypted with ``secret``."""
    backend = Backend(
        url=body.url,
        label=body.label,
        api_key_enc=encrypt_backend_key(body.api_key, secret) if body.api_key else None,
        max_inflight=body.max_inflight,
        priority=body.priority,
        device=body.device,
        adapter=body.adapter,
        managed=body.managed,
    )
    db.add(backend)
    await _commit_backend(db)
    await db.refresh(backend)
    return backend


async def upsert_backend(db: AsyncSession, body: BackendCreate, secret: str) -> Backend:
    """Create the backend keyed by ``url``, or update it in place. Idempotent, so the
    Helm hook runs it on every deploy. ``priority`` is never written here: it's set by
    hand (``backend update``) and survives redeploys. Omitting the API key keeps the
    stored one; an empty string clears it."""
    backend = await db.scalar(select(Backend).where(Backend.url == body.url))
    if backend is None:
        backend = Backend(url=body.url)
        db.add(backend)
    backend.label = body.label
    backend.max_inflight = body.max_inflight
    backend.device = body.device
    backend.adapter = body.adapter
    backend.managed = body.managed
    if body.api_key is not None:
        backend.api_key_enc = encrypt_backend_key(body.api_key, secret) if body.api_key else None
    await _commit_backend(db)
    await db.refresh(backend)
    if body.declared:
        await apply_declared_capabilities(db, backend.id, body.declared)
        await db.commit()
    return backend


async def update_backend(
    db: AsyncSession, backend_id: int, body: BackendUpdate, secret: str
) -> Backend:
    """Apply the fields present in ``body``. An API key in it is (re)encrypted; an empty
    one clears the stored key."""
    backend = await _backend(db, backend_id)
    data = body.model_dump(exclude_unset=True)
    if "api_key" in data:
        api_key = data.pop("api_key")
        data["api_key_enc"] = encrypt_backend_key(api_key, secret) if api_key else None
    for field, value in data.items():
        setattr(backend, field, value)
    await _commit_backend(db)
    await db.refresh(backend)
    return backend


async def delete_backend(db: AsyncSession, backend_id: int) -> None:
    """Delete a backend. Refused while jobs reference it; disable it to take it out of
    rotation instead."""
    backend = await _backend(db, backend_id)
    job_count = await db.scalar(
        select(func.count()).select_from(Job).where(Job.backend_id == backend_id)
    )
    if job_count:
        raise Conflict(f"Backend {backend_id} still has {job_count} job(s); cannot delete")
    await db.delete(backend)
    await db.commit()


# --- Domains ---


async def list_domains(db: AsyncSession) -> list[Domain]:
    result = await db.execute(select(Domain).order_by(Domain.name))
    return list(result.scalars().all())


async def update_domain(db: AsyncSession, name: str, **fields: Any) -> Domain:
    """Set a domain's metadata (label, description, legacy_engine_id, …). A domain only
    exists once some engine reported it, so an unknown name is NotFound."""
    domain = await db.scalar(select(Domain).where(Domain.name == name))
    if domain is None:
        raise NotFound(f"Domain {name!r} not found")
    for field, value in fields.items():
        setattr(domain, field, value)
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        raise Conflict(f"Another domain already uses that value: {e.orig}") from e
    await db.refresh(domain)
    return domain


# --- Config ---


async def get_config(db: AsyncSession) -> dict[str, Any]:
    return await config_service.get_all(db)


async def set_config(db: AsyncSession, values: dict[str, Any]) -> None:
    if not values:
        raise BadRequest("Nothing to set")
    await config_service.set_values(db, values)


# --- Jobs ---


async def list_failed_jobs(db: AsyncSession, limit: int = 50) -> list[Job]:
    """The most recently failed jobs (their rows are kept for the failed-job TTL)."""
    result = await db.execute(
        select(Job)
        .where(Job.status == JobStatus.FAILED)
        .order_by(Job.finished_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_job(db: AsyncSession, job_id: uuid.UUID) -> Job:
    job = await db.scalar(select(Job).where(Job.id == job_id))
    if job is None:
        raise NotFound(f"Job {job_id} not found")
    return job
