import httpx
import pytest

from app.services.engine_client import EngineClient


def _client_with(handler) -> EngineClient:
    ec = EngineClient()
    ec._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return ec


@pytest.mark.asyncio
async def test_get_version_reads_openapi_info_version():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        assert request.url.path == "/openapi.json"
        return httpx.Response(200, json={"info": {"title": "tuzkaocr", "version": "1.2.0"}})

    ec = _client_with(handler)
    assert await ec.get_version("http://engine") == "1.2.0"
    # Second lookup is served from the per-url cache, not a new HTTP request.
    assert await ec.get_version("http://engine") == "1.2.0"
    assert calls["n"] == 1
    await ec.close()


@pytest.mark.asyncio
async def test_get_version_is_none_on_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    ec = _client_with(handler)
    assert await ec.get_version("http://engine") is None
    await ec.close()
