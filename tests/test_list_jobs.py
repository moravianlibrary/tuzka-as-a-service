"""GET /api/v1/jobs input checks that run before the query."""

import pytest

from app.exceptions import Unprocessable
from app.routers.jobs import list_jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["nul\x00byte", "line\nbreak"])
async def test_unprintable_external_id_filter_is_422_not_a_db_error(label) -> None:
    # Postgres rejects a NUL in text, which was a 500; submit refuses such labels anyway.
    with pytest.raises(Unprocessable):
        await list_jobs(
            status=None, external_id=label, limit=50, offset=0, username="alice", db=None
        )
