"""Pins engine version lookup: reads info.version from OpenAPI and returns None on error (best-effort)."""

import httpx
import pytest

from app.engines.tuzkaocr import TuzkaOcrAdapter


def _make_adapter(handler) -> tuple[TuzkaOcrAdapter, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = TuzkaOcrAdapter(url="http://engine", api_key=None, http=http)
    return adapter, http


@pytest.mark.asyncio
async def test_version_reads_openapi_info_version():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.path == "/openapi.json"
        return httpx.Response(200, json={"info": {"title": "tuzkaocr", "version": "1.2.0"}})

    adapter, http = _make_adapter(handler)
    try:
        # Fetched on first use (dispatch needs it before any domain sync ran).
        assert await adapter.version() == "1.2.0"
        # Second lookup is served from the cache, not a new HTTP request.
        assert await adapter.version() == "1.2.0"
        assert calls["n"] == 1
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_version_is_none_on_error_and_cached():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    adapter, http = _make_adapter(handler)
    try:
        assert await adapter.version() is None
        # A failed lookup is cached too, so dispatch doesn't retry it per job.
        assert await adapter.version() is None
        assert calls["n"] == 1
    finally:
        await http.aclose()
