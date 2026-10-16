"""Engine adapter registry."""

from __future__ import annotations

import httpx

from app.engines.tuzkaocr import TuzkaOcrAdapter

ADAPTERS: dict[str, type[TuzkaOcrAdapter]] = {"tuzkaocr": TuzkaOcrAdapter}


def make_adapter(
    adapter_id: str,
    url: str,
    api_key: str | None,
    http: httpx.AsyncClient,
) -> TuzkaOcrAdapter:
    """Create an engine adapter instance by its registered id."""
    cls = ADAPTERS.get(adapter_id)
    if cls is None:
        raise KeyError(f"Unknown adapter id: {adapter_id!r}")
    return cls(url=url, api_key=api_key, http=http)
