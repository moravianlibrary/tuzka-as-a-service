"""SQLAlchemy declarative base + async engine/session wiring."""

# Timestamp columns are naive UTC by convention (TIMESTAMP WITHOUT TIME ZONE); see
# app.clock for the rationale, and use app.clock.utcnow() for taas-clock stamps.
from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.config import Settings

settings = Settings()


def make_engine(settings: Settings) -> AsyncEngine:
    """Build the async engine with pool hardening against stale connections.

    Long-lived processes (the app, the workers) keep a connection pool between DB
    calls; those connections can be closed under them by the server or a proxy. See
    Settings.db_pool_pre_ping for the failure mode this guards against.
    """
    return create_async_engine(
        settings.database_url,
        pool_pre_ping=settings.db_pool_pre_ping,
        pool_recycle=settings.db_pool_recycle_seconds,
    )


engine = make_engine(settings)
async_session = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db() -> AsyncGenerator[AsyncSession]:
    async with async_session() as session:
        yield session
