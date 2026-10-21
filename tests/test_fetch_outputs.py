"""Tests for fetch_outputs: a job delivers every output it asked for, or nothing.

A fake adapter stands in for the engine, so this runs without I/O.
"""

import pytest

from app.engines.base import OutputUnavailable, Result
from app.workers.poller import fetch_outputs


class _FakeAdapter:
    def __init__(self, unavailable: set[str] | None = None) -> None:
        self.unavailable = unavailable or set()
        self.fetched: list[str] = []

    async def fetch(self, engine_job_id: str, output: str) -> Result:
        self.fetched.append(output)
        if output in self.unavailable:
            raise OutputUnavailable(output, "georeferencing failed")
        return Result(content=output.encode(), content_type="text/plain")


@pytest.mark.asyncio
async def test_fetches_every_output_in_order() -> None:
    adapter = _FakeAdapter()
    outputs = frozenset({"json", "alto", "geojson"})
    got = await fetch_outputs(adapter, "e1", outputs)  # type: ignore[arg-type]
    assert got == {"alto": b"alto", "geojson": b"geojson", "json": b"json"}
    assert adapter.fetched == ["alto", "geojson", "json"]


@pytest.mark.asyncio
async def test_an_unavailable_output_means_nothing_is_returned() -> None:
    adapter = _FakeAdapter(unavailable={"gcp"})
    with pytest.raises(OutputUnavailable) as exc_info:
        await fetch_outputs(adapter, "e1", frozenset({"json", "gcp"}))  # type: ignore[arg-type]
    assert str(exc_info.value) == "gcp unavailable: georeferencing failed"
