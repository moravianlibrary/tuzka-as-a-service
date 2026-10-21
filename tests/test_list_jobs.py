"""GET /api/v1/jobs input checks that run before the query."""

import pytest

from app.routers.jobs import list_jobs


@pytest.mark.asyncio
@pytest.mark.parametrize("label", ["nul\x00byte", "line\nbreak"])
async def test_unprintable_external_id_filter_matches_nothing(label) -> None:
    # Submit refuses such labels; and a NUL sent to Postgres was a 500. No query runs.
    resp = await list_jobs(
        status=None, external_id=label, limit=50, offset=0, username="alice", db=None
    )
    assert resp.jobs == [] and resp.total == 0
