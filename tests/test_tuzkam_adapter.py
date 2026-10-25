"""Tests for TuzkaMAdapter via httpx.MockTransport.

The OpenAPI fixture is what FastAPI generates for TuzkaM's ``POST /api/v1/process``
signature (every form field a string); the status payload is shaped like the final
job status in TuzkaM's ``api/jobs.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.engines.base import EngineFullError, OutputUnavailable
from app.engines.tuzkam.adapter import TuzkaMAdapter, tuzkam_metrics, tuzkam_options_schema

OPENAPI_FIXTURE = Path(__file__).parent / "fixtures" / "tuzkam_openapi.json"

MAP_OUTPUTS = frozenset({"json", "geojson", "gcp", "alto", "txt"})

DONE_STATUS = {
    "job_id": "abc",
    "status": "done",
    "created_at": "2026-10-20T10:00:00+00:00",
    "started_at": "2026-10-20T10:00:01+00:00",
    "finished_at": "2026-10-20T10:02:41+00:00",
    "updated_at": "2026-10-20T10:02:41+00:00",
    "stage": "done",
    "progress": 1.0,
    "current": 412,
    "total": 412,
    "eta_s": 0.0,
    "n_words": 412,
    "mean_conf": 0.87,
    "georef": {
        "status": "ok",
        "control_points": 31,
        "inliers": 24,
        "residual_m": 812.5,
        "gazetteer_id": "europe-med",
    },
    "error": None,
    "error_status": None,
}


def _openapi() -> dict[str, Any]:
    result: dict[str, Any] = json.loads(OPENAPI_FIXTURE.read_text())
    return result


def _make_client(handler) -> tuple[TuzkaMAdapter, httpx.AsyncClient]:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return TuzkaMAdapter(url="http://tuzkam", api_key="k", http=http), http


def _form(request: httpx.Request) -> dict[str, str]:
    """The non-file fields of a multipart request."""
    fields: dict[str, str] = {}
    for part in request.read().decode("utf-8", "replace").split("--"):
        if 'name="' in part and "filename=" not in part:
            name = part.split('name="', 1)[1].split('"', 1)[0]
            fields[name] = part.split("\r\n\r\n", 1)[1].rsplit("\r\n", 1)[0]
    return fields


# ---- options schema + metrics (pure) ----


def test_options_schema_types_the_string_fields() -> None:
    schema = tuzkam_options_schema(_openapi())
    assert schema is not None
    props = schema["properties"]
    assert "image" not in props
    assert props["georef"] == {"type": "boolean"}
    assert props["alto_profile"] == {"enum": ["ndk", "basic"]}
    assert props["physical_img_nr"] == {"type": "integer", "minimum": 1}
    assert schema["additionalProperties"] is False


def test_options_schema_without_openapi() -> None:
    assert tuzkam_options_schema(None) is None


def test_metrics_flatten_the_georef_summary() -> None:
    assert tuzkam_metrics(DONE_STATUS) == {
        "n_words": 412,
        "mean_conf": 0.87,
        "progress": 1.0,
        "eta_s": 0.0,
        "georef.status": "ok",
        "georef.control_points": 31,
        "georef.inliers": 24,
        "georef.residual_m": 812.5,
    }
    assert tuzkam_metrics({"status": "running", "georef": None, "n_words": None}) == {}


# ---- validate_request (D3) ----


@pytest.mark.parametrize(
    ("outputs", "options", "rejected"),
    [
        ({"geojson"}, {"georef": False}, True),
        ({"json", "gcp"}, {"georef": False}, True),
        ({"geojson"}, {}, False),  # georef defaults to true on the engine
        ({"geojson"}, {"georef": True}, False),
        ({"json", "alto"}, {"georef": False}, False),
    ],
)
def test_validate_request(outputs, options, rejected) -> None:
    reason = TuzkaMAdapter.validate_request("maps", frozenset(outputs), options)
    assert (reason is not None) is rejected


# ---- describe ----


@pytest.mark.asyncio
async def test_describe_reports_the_configured_domains() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/openapi.json"
        return httpx.Response(200, json=_openapi())

    adapter, http = _make_client(handler)
    try:
        info = await adapter.describe()
    finally:
        await http.aclose()
    assert set(info.domains) == {"maps"}
    assert info.domains["maps"].outputs == MAP_OUTPUTS
    assert info.domains["maps"].options_schema is not None
    assert info.version == "0.4.0"


@pytest.mark.asyncio
async def test_describe_unreachable_reports_nothing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    adapter, http = _make_client(handler)
    try:
        info = await adapter.describe()
    finally:
        await http.aclose()
    assert info.domains == {}


# ---- submit ----


@pytest.mark.asyncio
async def test_submit_sends_image_and_options_only() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/process"
        assert request.headers["X-API-Key"] == "k"
        seen.update(_form(request))
        return httpx.Response(200, json={"job_id": "abc", "status": "queued"})

    adapter, http = _make_client(handler)
    try:
        job_id = await adapter.submit(
            b"img",
            "sheet.jpg",
            "maps",
            frozenset({"json", "alto"}),
            {"georef": False, "alto_profile": "basic", "physical_img_nr": 3, "region": None},
        )
    finally:
        await http.aclose()
    assert job_id == "abc"
    assert seen == {"georef": "false", "alto_profile": "basic", "physical_img_nr": "3"}


@pytest.mark.asyncio
async def test_submit_503_is_engine_full() -> None:
    adapter, http = _make_client(lambda request: httpx.Response(503))
    try:
        with pytest.raises(EngineFullError):
            await adapter.submit(b"img", "sheet.jpg", "maps", frozenset({"json"}), {})
    finally:
        await http.aclose()


# ---- status ----


@pytest.mark.asyncio
async def test_status_maps_a_finished_job() -> None:
    adapter, http = _make_client(lambda request: httpx.Response(200, json=DONE_STATUS))
    try:
        status = await adapter.status("abc")
    finally:
        await http.aclose()
    assert status.state == "done"
    assert status.started_at == DONE_STATUS["started_at"]
    assert status.error_status is None
    assert status.metrics["georef.residual_m"] == 812.5


@pytest.mark.asyncio
async def test_status_reads_error_status_natively() -> None:
    payload = {**DONE_STATUS, "status": "failed", "error": "bad image", "error_status": 422}
    adapter, http = _make_client(lambda request: httpx.Response(200, json=payload))
    try:
        status = await adapter.status("abc")
    finally:
        await http.aclose()
    assert (status.state, status.error, status.error_status) == ("failed", "bad image", 422)


# ---- fetch ----


@pytest.mark.asyncio
async def test_fetch_asks_for_the_output_by_fmt() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["fmt"] == "geojson"
        body = b'{"type":"FeatureCollection"}'
        return httpx.Response(200, content=body, headers={"content-type": "application/geo+json"})

    adapter, http = _make_client(handler)
    try:
        result = await adapter.fetch("abc", "geojson")
    finally:
        await http.aclose()
    assert result.content_type == "application/geo+json"


@pytest.mark.asyncio
async def test_fetch_409_is_output_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        detail = "Georeferencing failed; QGIS points are unavailable"
        return httpx.Response(409, json={"detail": detail})

    adapter, http = _make_client(handler)
    try:
        with pytest.raises(OutputUnavailable) as exc_info:
            await adapter.fetch("abc", "gcp")
    finally:
        await http.aclose()
    assert exc_info.value.output == "gcp"
    assert "Georeferencing failed" in exc_info.value.reason
    assert str(exc_info.value).startswith("gcp unavailable: ")
