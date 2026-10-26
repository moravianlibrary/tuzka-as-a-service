import os
from collections.abc import AsyncIterator

import pytest
import redis.asyncio as aioredis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.services import config as config_service

# db 15 keeps test keys away from the dev stack's db 0
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")
# A throwaway migrated Postgres: every test that uses it starts from empty tables.
TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
_TABLES = "batches, backend_domains, domains, job_results, jobs, backends, users, config"


@pytest.fixture
async def redis_client():
    r = aioredis.from_url(TEST_REDIS_URL, decode_responses=False)
    try:
        await r.ping()
    except (RedisError, OSError):
        pytest.skip("Redis not reachable — run `docker compose up -d redis`")
    await r.flushdb()
    yield r
    await r.flushdb()
    await r.aclose()


@pytest.fixture
async def database() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Sessions on the test database, its tables emptied; skips the test without one.
    The config cache is cleared too, since it can't see the TRUNCATE."""
    if not TEST_DATABASE_URL:
        pytest.skip("Set TEST_DATABASE_URL to a throwaway migrated DB to run these tests")
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("TEST_DATABASE_URL not reachable")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        await db.execute(text(f"TRUNCATE {_TABLES} RESTART IDENTITY CASCADE"))
        await db.commit()
    config_service.clear_cache()
    yield factory
    config_service.clear_cache()
    await engine.dispose()


@pytest.fixture
async def session_factory(database) -> async_sessionmaker[AsyncSession]:
    """The emptied test database (``database``) with the user ``alice``."""
    async with database() as db:
        await db.execute(text("INSERT INTO users (username, hashed_key) VALUES ('alice', 'x')"))
        await db.commit()
    return database


@pytest.fixture
async def session(session_factory) -> AsyncIterator[AsyncSession]:
    """One session on the emptied test database, with ``alice`` (``session_factory``)."""
    async with session_factory() as db:
        yield db
