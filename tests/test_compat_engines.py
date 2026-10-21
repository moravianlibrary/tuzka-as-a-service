"""Legacy engine ids from taas /api/v1/domains: mapping, per-key cache, unknown id."""

import httpx
import pytest
from fastapi import HTTPException

from compat.app import engines as engines_mod
from compat.app.engines import engines_from_domains, get_engine_map, resolve_engine

DOMAINS = {
    "domains": [
        {
            "name": "kramarky",
            "label": "Kramarky",
            "description": "Engine for kramarky domain",
            "outputs": ["alto", "txt"],
            "options_schema": None,
            "legacy_engine_id": 2,
        },
        {
            "name": "default",
            "label": "Default",
            "description": "Default OCR engine",
            "outputs": ["alto", "txt"],
            "options_schema": None,
            "legacy_engine_id": 1,
        },
        {
            "name": "handwritten",
            "label": None,
            "description": None,
            "outputs": ["alto", "txt"],
            "options_schema": None,
            "legacy_engine_id": None,
        },
    ]
}


@pytest.fixture(autouse=True)
def _clear_cache():
    engines_mod._engines_cache.clear()
    yield
    engines_mod._engines_cache.clear()


def make_client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://taas")


def test_mapping_orders_by_id_and_skips_domains_without_legacy_id():
    engines = engines_from_domains(DOMAINS)
    assert list(engines) == [1, 2]
    assert engines[1].domain == "default"
    assert engines[1].label == "Default"
    assert engines[1].description == "Default OCR engine"
    assert engines[2].domain == "kramarky"


def test_label_falls_back_to_name():
    payload = {"domains": [{"name": "maps", "label": None, "legacy_engine_id": 3}]}
    engines = engines_from_domains(payload)
    assert engines[3].label == "maps"
    assert engines[3].description is None


async def test_cache_is_per_key():
    calls: list[str] = []

    async def handler(request):
        calls.append(request.headers["X-API-Key"])
        return httpx.Response(200, json=DOMAINS)

    async with make_client(handler) as client:
        await get_engine_map(client, "key-a")
        await get_engine_map(client, "key-a")
        await get_engine_map(client, "key-b")
    assert calls == ["key-a", "key-b"]


async def test_cache_expires(monkeypatch):
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(200, json=DOMAINS)

    async with make_client(handler) as client:
        await get_engine_map(client, "key-a")
        monkeypatch.setattr(engines_mod, "ENGINES_CACHE_TTL_SECONDS", -1.0)
        engines_mod._engines_cache.clear()
        await get_engine_map(client, "key-a")  # stores an already-expired entry
        await get_engine_map(client, "key-a")
    assert calls["n"] == 3


async def test_invalid_key_is_401_and_not_cached():
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(401, json={"detail": "Invalid API key"})

    async with make_client(handler) as client:
        for _ in range(2):
            with pytest.raises(HTTPException) as exc_info:
                await get_engine_map(client, "bad")
            assert exc_info.value.status_code == 401
    assert calls["n"] == 2


async def test_unknown_engine_id_is_400():
    async def handler(request):
        return httpx.Response(200, json=DOMAINS)

    async with make_client(handler) as client:
        assert (await resolve_engine(client, "key-a", 2)).domain == "kramarky"
        for bad in (7, "1", None):
            with pytest.raises(HTTPException) as exc_info:
                await resolve_engine(client, "key-a", bad)
            assert exc_info.value.status_code == 400
            assert "Unknown engine" in exc_info.value.detail
