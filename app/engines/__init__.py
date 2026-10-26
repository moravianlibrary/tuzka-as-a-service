"""Engine adapter registry."""

from __future__ import annotations

import httpx

from app.engines.base import EngineAdapter
from app.engines.tuzkam.adapter import TuzkaMAdapter
from app.engines.tuzkaocr.adapter import TuzkaOcrAdapter
from app.vocabulary import AdapterId

ADAPTERS: dict[AdapterId, type[TuzkaOcrAdapter] | type[TuzkaMAdapter]] = {
    AdapterId.TUZKAOCR: TuzkaOcrAdapter,
    AdapterId.TUZKAM: TuzkaMAdapter,
}


def make_adapter(
    adapter_id: AdapterId,
    url: str,
    api_key: str | None,
    http: httpx.AsyncClient,
) -> EngineAdapter:
    """Create an engine adapter instance by its registered id."""
    return ADAPTERS[adapter_id](url=url, api_key=api_key, http=http)
