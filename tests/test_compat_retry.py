import httpx
import pytest
from fastapi import HTTPException

from compat.app import retry as retry_mod
from compat.app.retry import request_with_retry


def make_client(handler):
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://taas"
    )


async def test_passes_through_non_429():
    async def handler(request):
        return httpx.Response(200, json={"ok": True})

    async with make_client(handler) as client:
        resp = await request_with_retry(client, "GET", "/api/v1/jobs")
    assert resp.status_code == 200


async def test_retries_429_until_success():
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(429, headers={"Retry-After": "0"})
        return httpx.Response(200, json={"ok": True})

    async with make_client(handler) as client:
        resp = await request_with_retry(client, "GET", "/api/v1/jobs")
    assert resp.status_code == 200
    assert calls["n"] == 3


async def test_budget_exhaustion_raises_503(monkeypatch):
    monkeypatch.setattr(retry_mod, "RETRY_BUDGET_SECONDS", 0.3)
    monkeypatch.setattr(retry_mod, "MAX_SLEEP_PER_ATTEMPT", 0.1)

    async def handler(request):
        return httpx.Response(429, headers={"Retry-After": "1"})

    async with make_client(handler) as client:
        with pytest.raises(HTTPException) as exc_info:
            await request_with_retry(client, "GET", "/api/v1/jobs")
    assert exc_info.value.status_code == 503
    assert "busy" in exc_info.value.detail.lower()


async def test_non_429_errors_are_not_retried():
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return httpx.Response(500, text="boom")

    async with make_client(handler) as client:
        resp = await request_with_retry(client, "GET", "/api/v1/jobs")
    assert resp.status_code == 500
    assert calls["n"] == 1
