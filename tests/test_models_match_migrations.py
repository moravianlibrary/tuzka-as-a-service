"""The models describe the schema the migrations build, column type for column type, so
`make db-migrate` autogenerates only what a change really needs. Against the throwaway
migrated Postgres (``TEST_DATABASE_URL``); skips otherwise."""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext

from app.models import Base


@pytest.mark.asyncio
async def test_autogenerate_finds_nothing_to_change(database) -> None:
    engine = database.kw["bind"]
    async with engine.connect() as conn:
        diff = await conn.run_sync(
            lambda sync: compare_metadata(MigrationContext.configure(sync), Base.metadata)
        )

    assert diff == []
