"""Legacy engine ids, read from taas's ``GET /api/v1/domains``.

Each domain with a ``legacy_engine_id`` is one legacy engine. The map is cached per
API key (by hash), because what ``/domains`` returns may depend on the key's scope.
"""

import hashlib
import time
from typing import Any

import httpx
from fastapi import HTTPException

from .config import EngineConfig
from .retry import request_with_retry

ENGINES_CACHE_TTL_SECONDS = 60.0

_engines_cache: dict[str, tuple[float, dict[int, EngineConfig]]] = {}


def engines_from_domains(payload: dict[str, Any]) -> dict[int, EngineConfig]:
    """Map a ``/api/v1/domains`` response to ``legacy engine id → EngineConfig``,
    ordered by id. Domains without a legacy id aren't exposed to legacy clients."""
    engines: dict[int, EngineConfig] = {}
    for entry in payload.get("domains", []):
        engine_id = entry.get("legacy_engine_id")
        if engine_id is None:
            continue
        engines[int(engine_id)] = EngineConfig(
            domain=entry["name"],
            label=entry.get("label") or entry["name"],
            description=entry.get("description"),
        )
    return dict(sorted(engines.items()))


async def get_engine_map(http: httpx.AsyncClient, api_key: str) -> dict[int, EngineConfig]:
    """The legacy engines visible to ``api_key``, cached for a minute.

    ``/api/v1/domains`` is a protected endpoint, so a cache miss also validates the key.
    """
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    now = time.monotonic()
    cached = _engines_cache.get(key_hash)
    if cached and now < cached[0]:
        return cached[1]

    resp = await request_with_retry(http, "GET", "/api/v1/domains", headers={"X-API-Key": api_key})
    if resp.status_code == 401:
        raise HTTPException(401, "Invalid api-key")
    if resp.status_code != 200:
        raise HTTPException(502, "Failed to list engines")

    engines = engines_from_domains(resp.json())
    _engines_cache[key_hash] = (now + ENGINES_CACHE_TTL_SECONDS, engines)
    return engines


async def resolve_engine(http: httpx.AsyncClient, api_key: str, engine_id: Any) -> EngineConfig:
    """The engine for a legacy ``engine`` id; 400 if the caller can't use it."""
    engines = await get_engine_map(http, api_key)
    cfg = engines.get(engine_id) if isinstance(engine_id, int) else None
    if cfg is None:
        raise HTTPException(400, f"Unknown engine: {engine_id}")
    return cfg
