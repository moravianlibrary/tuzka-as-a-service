"""Engine adapters for the workers, built once per backend and reused across ticks."""

from __future__ import annotations

import httpx

from app.engines import make_adapter
from app.engines.base import EngineAdapter
from app.models.backend import Backend
from app.services.auth import decrypt_backend_key
from app.vocabulary import AdapterId


class AdapterCache:
    """Adapters keyed by what they're built from (adapter id, URL, encrypted key), so a
    backend re-registered with a new URL or key gets a new adapter on its next use."""

    def __init__(self, http: httpx.AsyncClient, key_encryption_secret: str) -> None:
        self._http = http
        self._secret = key_encryption_secret
        self._adapters: dict[tuple[AdapterId, str, str | None], EngineAdapter] = {}

    def for_backend(self, backend: Backend | None, url: str) -> EngineAdapter:
        """The adapter for *backend* at *url*; a backend that's gone (its row deleted
        while its jobs are still in flight) is talked to as a keyless TuzkaOCR."""
        adapter_id = backend.adapter if backend else AdapterId.TUZKAOCR
        api_key_enc = backend.api_key_enc if backend else None
        source = (adapter_id, url, api_key_enc)
        if source not in self._adapters:
            api_key = decrypt_backend_key(api_key_enc, self._secret) if api_key_enc else None
            self._adapters[source] = make_adapter(adapter_id, url, api_key, self._http)
        return self._adapters[source]
