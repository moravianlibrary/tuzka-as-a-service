"""Tests for TuzkaOcrAdapter via httpx.MockTransport."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.engines.base import EngineFullError
from app.engines.openapi import options_schema_from_openapi
from app.engines.tuzkaocr import TuzkaOcrAdapter

# What FastAPI + pydantic v2 generate for TuzkaOCR's POST /api/v1/process signature.
OPENAPI_FIXTURE = Path(__file__).parent / "fixtures" / "tuzkaocr_openapi.json"


def _openapi() -> dict[str, Any]:
    result: dict[str, Any] = json.loads(OPENAPI_FIXTURE.read_text())
    return result


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
                b"img", "page.jpg", None, frozenset({"alto", "txt"}), {}
            )
            assert result == "job-123"
        finally:
            await http.aclose()

    @pytest.mark.asyncio
    async def test_submit_alto(self) -> None:
        adapter, http = _make_client(self._handler_factory("alto"))
        try:
            result = await adapter.submit(
                b"img", "page.jpg", None, frozenset({"alto"}), {}
            )
            assert result == "job-123"
        finally:
            await http.aclose()

    @pytest.mark.asyncio
    async def test_submit_txt(self) -> None:
        adapter, http = _make_client(self._handler_factory("txt"))
        try:
            result = await adapter.submit(b"img", "page.jpg", None, frozenset({"txt"}), {})
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
                b"img", "page.jpg", "handwritten", frozenset({"alto", "txt"}), {}
            )
        finally:
            await http.aclose()

    @pytest.mark.asyncio
    async def test_submit_default_domain_sends_no_domain_field(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            body = request.read().decode("utf-8")  # type: ignore[union-attr]
            assert 'name="domain"' not in body
            return httpx.Response(200, json={"job_id": "job-123"})

        adapter, http = _make_client(handler)
        try:
            await adapter.submit(b"img", "page.jpg", "default", frozenset({"alto", "txt"}), {})
        finally:
            await http.aclose()

    @pytest.mark.asyncio
    async def test_submit_sends_options_as_strings(self) -> None:
        sent: dict[str, str] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            body = request.read().decode("utf-8")  # type: ignore[union-attr]
            for part in body.split("--"):
                if 'name="' in part and "filename=" not in part:
                    name = part.split('name="', 1)[1].split('"', 1)[0]
                    sent[name] = part.split("\r\n\r\n", 1)[1].rsplit("\r\n", 1)[0]
            return httpx.Response(200, json={"job_id": "job-123"})

        adapter, http = _make_client(handler)
        try:
            await adapter.submit(
                b"img",
                "page.jpg",
                None,
                frozenset({"alto"}),
                {
                    "role_classifier": True,
                    "physical_img_nr": 5,
                    "alto_profile": "basic",
                    "source_identifier": None,
                },
            )
        finally:
            await http.aclose()
        assert sent["role_classifier"] == "true"
        assert sent["physical_img_nr"] == "5"
        assert sent["alto_profile"] == "basic"
        assert "source_identifier" not in sent  # None = not sent

    @pytest.mark.asyncio
    async def test_submit_503_raises_engine_full(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(503)

        adapter, http = _make_client(handler)
        try:
            with pytest.raises(EngineFullError):
                await adapter.submit(
                    b"img", "page.jpg", None, frozenset({"alto", "txt"}), {}
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
            await adapter.submit(b"img", "page.jpg", None, frozenset({"alto", "txt"}), {})
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
    async def test_describe_reads_capabilities(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "openapi.json" in str(request.url):
                return httpx.Response(200, json=_openapi())
            return httpx.Response(
                200,
                json={
                    "selectable_via_domain": ["default", "kramarky"],
                    # kramarky missing from capabilities -> fallback {alto, txt}
                    "capabilities": {"default": ["alto"]},
                },
            )

        adapter, http = _make_client(handler)
        try:
            info = await adapter.describe()
        finally:
            await http.aclose()
        assert info.domains["default"].outputs == frozenset({"alto"})
        assert info.domains["kramarky"].outputs == frozenset({"alto", "txt"})
        schema = info.domains["default"].options_schema
        assert schema is not None
        assert "alto_profile" in schema["properties"]
        assert info.domains["kramarky"].options_schema == schema

    @pytest.mark.asyncio
    async def test_describe_without_openapi_has_no_schema(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if "openapi.json" in str(request.url):
                return httpx.Response(500)
            return httpx.Response(200, json={"selectable_via_domain": ["default"]})

        adapter, http = _make_client(handler)
        try:
            info = await adapter.describe()
        finally:
            await http.aclose()
        assert info.domains["default"].outputs == frozenset({"alto", "txt"})
        assert info.domains["default"].options_schema is None
        assert info.version is None

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
            assert await adapter.version() == "1.7.0"
            assert calls["n"] == 1
        finally:
            await http.aclose()


# ---- options schema from OpenAPI ----


class TestOptionsSchemaFromOpenapi:
    def test_drops_reserved_fields_and_closes_object(self) -> None:
        schema = options_schema_from_openapi(_openapi())
        assert schema is not None
        assert set(schema["properties"]) == {
            "role_classifier",
            "source_identifier",
            "alto_profile",
            "physical_img_nr",
        }
        assert schema["additionalProperties"] is False
        assert schema["type"] == "object"
        assert "required" not in schema  # only `image` was required, and it's reserved

    def test_keeps_option_types(self) -> None:
        schema = options_schema_from_openapi(_openapi())
        assert schema is not None
        types = schema["properties"]["physical_img_nr"]["anyOf"]
        assert {"type": "integer"} in types

    def test_inline_body_schema(self) -> None:
        spec = _openapi()
        body = spec["components"]["schemas"]["Body_process_image_api_v1_process_post"]
        content = spec["paths"]["/api/v1/process"]["post"]["requestBody"]["content"]
        content["multipart/form-data"]["schema"] = body
        schema = options_schema_from_openapi(spec)
        assert schema is not None
        assert "alto_profile" in schema["properties"]

    def test_missing_process_path_is_none(self) -> None:
        assert options_schema_from_openapi({"info": {"version": "1"}, "paths": {}}) is None
