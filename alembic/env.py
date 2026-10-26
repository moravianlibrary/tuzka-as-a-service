import asyncio
from logging.config import fileConfig
from typing import Any

from alembic.autogenerate.api import AutogenContext
from sqlalchemy import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context
from app.config import Settings

# Importing Base pulls in every model via app.models' package init, so
# Base.metadata is complete for autogenerate.
from app.models import Base
from app.models.types import PydanticJSONBList, StrEnumType

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
settings = Settings()


def render_item(kind: str, obj: Any, autogen_context: AutogenContext) -> str | bool:
    """Write taas's column types into a migration as the plain types they store, so a
    migration never imports app code (a text column for an enum, JSONB for a list of
    models)."""
    if kind != "type":
        return False
    if isinstance(obj, StrEnumType):
        return "sa.Text()"
    if isinstance(obj, PydanticJSONBList):
        autogen_context.imports.add("from sqlalchemy.dialects import postgresql")
        return "postgresql.JSONB(astext_type=sa.Text())"
    return False


def run_migrations_offline() -> None:
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        render_item=render_item,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection, target_metadata=target_metadata, render_item=render_item
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = create_async_engine(settings.database_url)
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
