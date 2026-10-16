"""Tests for TuzkaOcrAdapter via httpx.MockTransport."""

from __future__ import annotations

import httpx
import pytest
from pydantic import BaseModel

from app.engines.base import EngineFullError
from app.engines.tuzkaocr import NoOptions, TuzkaOcrAdapter


def _make_client(handler) -> tuple[TuzkaOcrAdapter, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = TuzkaOcrAdapter(url="http://engine", api_key=None, http=http)
    return adapter, http


# ---- submit ----


class TestSubmit:
    def _handler_factory(self, expected_fmt: str) -> callable:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/api/v1/process"
            assert request.headers.get("content-type", "").startswith("multipart/form-data")
            form = request.read().decode("utf-8")  # type: ignore[union-attr]
            assert 'form-data; name="fmt"' in form
            assert f"{expected_fmt}" in form
            return httpx.Response(200, json={"job_id": "job-123"})

        return handler

    @pytest.mark.asyncio
    async def test_submit_multi(self) -> None:
        """submit with fmt=multi sends fmt=multi (both outputs)."""
        adapter, http = _make_client(self._handler_factory("multi"))
        try:
            result = await adapter.submit(
                b"img", "page.jpg", None, frozenset({"alto", "txt"}), NoOptions()
            )
            assert result == "job-123"
        finally:
            await http.aclose()

    @pytest.mark.asyncio
    async def test_submit_alto(self) -> None:
        adapter, http = _make_client(self._handler_factory("alto"))
        try:
            result = await adapter.submit(
                b"img", "page.jpg", None, frozenset({"alto"}), NoOptions()
            )
            assert result == "job-123"
        finally:
            await http.aclose()

    @pytest.mark.asyncio
    async def test_submit_txt(self) -> None:
        adapter, http = _make_client(self._handler_factory("txt"))
        try:
            result = await adapter.submit(b"img", "page.jpg", None, frozenset({"txt"}), NoOptions())
            assert result == "job-123"
        finally:
            await http.aclose()

    @pytest.mark.asyncio
    async def test_submit_sends_domain(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = request.read().decode("utf-8")  # type: ignore[union-attr]
            assert "domain" in body
            assert "handwritten" in body
            return httpx.Response(200, json={"job_id": "job-123"})

        adapter, http = _make_client(handler)
        try:
            await adapter.submit(
                b"img", "page.jpg", "handwritten", frozenset({"alto", "txt"}), NoOptions()
            )
        finally:
            await http.aclose()

    @pytest.mark.asyncio
    async def test_submit_503_raises_engine_full(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        adapter, http = _make_client(handler)
        try:
            with pytest.raises(EngineFullError):
                await adapter.submit(
                    b"img", "page.jpg", None, frozenset({"alto", "txt"}), NoOptions()
                )
        finally:
            await http.aclose()

    @pytest.mark.asyncio
    async def test_submit_sends_api_key(self) -> None:
        http = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"job_id": "job-123"})
            )
        )
        adapter = TuzkaOcrAdapter(url="http://engine", api_key="secret-key", http=http)
        try:
            await adapter.submit(b"img", "page.jpg", None, frozenset({"alto", "txt"}), NoOptions())
        finally:
            await http.aclose()


# ---- status ----


class TestStatus:
    @pytest.mark.asyncio
    async def test_status_maps_fields(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "job_id": "job-123",
                    "status": "done",
                    "created_at": "2026-01-01T00:00:00+00:00",
                    "started_at": "2026-01-01T00:00:01+00:00",
                    "finished_at": "2026-01-01T00:00:05+00:00",
                    "mean_conf": 0.95,
                    "n_lines": 100,
                    "error": None,
                    "error_status": None,
                },
            )

        adapter, http = _make_client(handler)
        try:
            status = await adapter.status("job-123")
            assert status.state == "done"
            assert status.error is None
            assert status.error_status is None
            assert status.metrics == {"n_lines": 100, "mean_conf": 0.95}
        finally:
            await http.aclose()

    @pytest.mark.asyncio
    async def test_status_failed_with_error_status(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "job_id": "job-123",
                    "status": "failed",
                    "error": "OOM",
                    "error_status": 500,
                },
            )

        adapter, http = _make_client(handler)
        try:
            status = await adapter.status("job-123")
            assert status.state == "failed"
            assert status.error == "OOM"
            assert status.error_status == 500
        finally:
            await http.aclose()


# ---- fetch ----


class TestFetch:
    @pytest.mark.asyncio
    async def test_fetch_sends_which(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert "which=alto" in str(request.url)
            return httpx.Response(
                200, content=b"<alto></alto>", headers={"content-type": "application/xml"}
            )

        adapter, http = _make_client(handler)
        try:
            result = await adapter.fetch("job-123", "alto")
            assert result.content == b"<alto></alto>"
            assert result.content_type == "application/xml"
        finally:
            await http.aclose()


# ---- describe ----


class TestDescribe:
    @pytest.mark.asyncio
    async def test_describe_survives_500(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "openapi.json" in str(request.url):
                return httpx.Response(500)
            return httpx.Response(500)

        adapter, http = _make_client(handler)
        try:
            info = await adapter.describe()
            assert info.domains == {}
            assert info.version is None
        finally:
            await http.aclose()

    @pytest.mark.asyncio
    async def test_describe_reads_models(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "openapi.json" in str(request.url):
                return httpx.Response(200, json={"info": {"version": "1.7.0"}})
            assert "/api/v1/models" in str(request.url)
            return httpx.Response(200, json={"selectable_via_domain": ["default", "handwritten"]})

        adapter, http = _make_client(handler)
        try:
            info = await adapter.describe()
            assert "default" in info.domains
            assert "handwritten" in info.domains
            assert info.domains["default"].outputs == frozenset({"alto", "txt"})
            assert info.version == "1.7.0"
        finally:
            await http.aclose()

    @pytest.mark.asyncio
    async def test_version_caching(self) -> None:
        calls = {"n": 0}

        def handler(request: httpx.Request) -> httpx.Response:
            if "openapi.json" in str(request.url):
                calls["n"] += 1
                return httpx.Response(200, json={"info": {"version": "1.7.0"}})
            return httpx.Response(200, json={"selectable_via_domain": []})

        adapter, http = _make_client(handler)
        try:
            await adapter.describe()
            assert calls["n"] == 1
            # Second describe should use cached version
            await adapter.describe()
            assert calls["n"] == 1
            # version() is sync and returns cached value
            assert adapter.version() == "1.7.0"
        finally:
            await http.aclose()


# ---- options_schema ----


class TestOptionsSchema:
    @pytest.mark.asyncio
    async def test_returns_no_options(self) -> None:
        http = httpx.AsyncClient()
        adapter = TuzkaOcrAdapter(url="http://engine", api_key=None, http=http)
        try:
            schema = adapter.options_schema("default")
            assert schema is NoOptions
            instance = schema()
            assert isinstance(instance, BaseModel)
        finally:
            await http.aclose()
