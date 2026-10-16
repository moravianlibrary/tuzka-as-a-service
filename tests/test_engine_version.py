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
    openapi_calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if "openapi.json" in str(request.url):
            openapi_calls["n"] += 1
            return httpx.Response(200, json={"info": {"title": "tuzkaocr", "version": "1.2.0"}})
        return httpx.Response(200, json={"selectable_via_domain": []})

    adapter, http = _make_adapter(handler)
    try:
        # version() returns None until describe() is called.
        assert adapter.version() is None
        # describe() fetches version from OpenAPI and caches it.
        await adapter.describe()
        assert adapter.version() == "1.2.0"
        assert openapi_calls["n"] == 1
        # Second describe uses cached version — no new OpenAPI call.
        await adapter.describe()
        assert openapi_calls["n"] == 1
    finally:
        await http.aclose()


@pytest.mark.asyncio
async def test_version_is_none_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        if "openapi.json" in str(request.url):
            return httpx.Response(503)
        return httpx.Response(200, json={"selectable_via_domain": []})

    adapter, http = _make_adapter(handler)
    try:
        info = await adapter.describe()
        assert info.version is None
    finally:
        await http.aclose()
