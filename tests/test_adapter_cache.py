"""Pins the workers' adapter cache: one adapter per (adapter id, URL, key), keyless
TuzkaOCR for a backend whose row is gone."""

import httpx
from cryptography.fernet import Fernet

from app.engines.cache import AdapterCache
from app.engines.tuzkam.adapter import TuzkaMAdapter
from app.engines.tuzkaocr.adapter import TuzkaOcrAdapter
from app.models.backend import Backend
from app.services.auth import encrypt_backend_key
from app.vocabulary import AdapterId

SECRET = Fernet.generate_key().decode()


def _backend(**fields) -> Backend:
    return Backend(id=1, url="http://engine", adapter=AdapterId.TUZKAOCR, **fields)


def test_reuses_the_adapter_until_its_source_changes() -> None:
    cache = AdapterCache(httpx.AsyncClient(), SECRET)
    backend = _backend()
    first = cache.for_backend(backend, backend.url)
    assert cache.for_backend(backend, backend.url) is first
    assert cache.for_backend(backend, "http://moved") is not first
    backend.adapter = AdapterId.TUZKAM
    assert isinstance(cache.for_backend(backend, backend.url), TuzkaMAdapter)


def test_decrypts_the_backend_key() -> None:
    cache = AdapterCache(httpx.AsyncClient(), SECRET)
    backend = _backend(api_key_enc=encrypt_backend_key("s3cret", SECRET))
    adapter = cache.for_backend(backend, backend.url)
    assert isinstance(adapter, TuzkaOcrAdapter)
    assert adapter._api_key == "s3cret"


def test_a_deleted_backend_is_a_keyless_tuzkaocr() -> None:
    adapter = AdapterCache(httpx.AsyncClient(), SECRET).for_backend(None, "http://engine")
    assert isinstance(adapter, TuzkaOcrAdapter)
    assert adapter._api_key is None
