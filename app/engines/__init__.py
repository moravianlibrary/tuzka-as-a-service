"""Engine adapter registry."""

from __future__ import annotations

import httpx

from app.engines.base import EngineAdapter
from app.engines.tuzkam import TuzkaMAdapter
from app.engines.tuzkaocr import TuzkaOcrAdapter

ADAPTERS: dict[str, type[TuzkaOcrAdapter] | type[TuzkaMAdapter]] = {
    "tuzkaocr": TuzkaOcrAdapter,
    "tuzkam": TuzkaMAdapter,
}


def make_adapter(
    adapter_id: str,
    url: str,
    api_key: str | None,
    http: httpx.AsyncClient,
) -> EngineAdapter:
    """Create an engine adapter instance by its registered id."""
    cls = ADAPTERS.get(adapter_id)
    if cls is None:
        raise KeyError(f"Unknown adapter id: {adapter_id!r}")
    return cls(url=url, api_key=api_key, http=http)
