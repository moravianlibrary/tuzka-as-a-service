"""TuzkaM engine adapter (OCR of historical map sheets, optional georeferencing)."""

from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Any

import httpx

from app.config import Settings
from app.engines.base import (
    DomainInfo,
    EngineFullError,
    EngineInfo,
    EngineStatus,
    OutputUnavailable,
    Result,
)
from app.engines.openapi import form_value, options_schema_from_openapi
from app.vocabulary import EngineState

logger = logging.getLogger(__name__)

_OPENAPI_CACHE_TTL_SECONDS = 300.0

# Everything TuzkaM's result endpoint renders (`?fmt=`), for every domain it serves.
_OUTPUTS = frozenset({"json", "geojson", "gcp", "alto", "txt"})

# Outputs that only exist for a georeferenced job.
_GEOREF_OUTPUTS = frozenset({"geojson", "gcp"})

# TuzkaM declares every form field as a string and parses it itself, so its OpenAPI
# schema can't tell a client that `georef` is a boolean. These overlay the derived
# properties with what the engine actually accepts, so a bad value is a 422 at submit.
_TYPED_OPTIONS: dict[str, dict[str, Any]] = {
    "georef": {"type": "boolean"},
    "alto_profile": {"enum": ["ndk", "basic"]},
    "physical_img_nr": {"type": "integer", "minimum": 1},
    "region": {"type": "string"},
    "source_identifier": {"type": "string"},
}

# Status keys copied into EngineStatus.metrics (D7/D8). The georef summary is flattened
# to dotted keys; `residual_m` is TuzkaM's RMS in meters.
_METRIC_KEYS = ("n_words", "mean_conf", "progress", "eta_s")
_GEOREF_METRIC_KEYS = ("status", "control_points", "inliers", "residual_m")


@lru_cache(maxsize=1)
def _configured_domains() -> tuple[str, ...]:
    return tuple(Settings().tuzkam_domains)


def tuzkam_options_schema(spec: dict[str, Any] | None) -> dict[str, Any] | None:
    """The per-job options schema: TuzkaM's form fields from its OpenAPI, with the
    precise types it parses them as. None when the OpenAPI isn't available."""
    schema = options_schema_from_openapi(spec) if spec else None
    if schema is None:
        return None
    properties = schema["properties"]
    for name, typed in _TYPED_OPTIONS.items():
        if name in properties:
            properties[name] = typed
    return schema


def tuzkam_metrics(data: dict[str, Any]) -> dict[str, int | float | str]:
    """The numbers (and georef status) worth keeping from a TuzkaM status payload."""
    metrics: dict[str, int | float | str] = {}
    for key in _METRIC_KEYS:
        value = data.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            metrics[key] = value
    georef = data.get("georef")
    if isinstance(georef, dict):
        for key in _GEOREF_METRIC_KEYS:
            value = georef.get(key)
            if isinstance(value, (int, float, str)) and not isinstance(value, bool):
                metrics[f"georef.{key}"] = value
    return metrics


class TuzkaMAdapter:
    def __init__(
        self,
        url: str,
        api_key: str | None,
        http: httpx.AsyncClient,
    ) -> None:
        self._url = url
        self._api_key = api_key
        self._http = http
        self._openapi_cache: dict[str, Any] | None = None
        self._openapi_cached_at: float = 0.0

    # ---- public ----

    @classmethod
    def validate_request(
        cls, domain: str, outputs: frozenset[str], options: dict[str, Any]
    ) -> str | None:
        """GeoJSON and GCPs need georeferencing; asking for them with ``georef=false``
        would only fail at the end (TuzkaM answers 409), so it's refused at submit."""
        wanted = sorted(outputs & _GEOREF_OUTPUTS)
        if wanted and options.get("georef") is False:
            verb = "needs" if len(wanted) == 1 else "need"
            return f"{' and '.join(wanted)} {verb} georef=true"
        return None

    async def healthy(self) -> bool:
        try:
            resp = await self._http.get(f"{self._url}/healthz", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            logger.warning("healthcheck failed for %s", self._url, exc_info=True)
            return False

    async def describe(self) -> EngineInfo:
        """The configured map domains (TuzkaM's /api/v1/models lists models, not
        domains), each with every output and the options schema from its OpenAPI.

        Reports nothing when the engine is unreachable, so domain sync keeps the
        remembered capabilities instead of linking a backend that isn't there.
        """
        spec = await self._openapi()
        if spec is None:
            return EngineInfo(domains={}, version=None)
        version = spec.get("info", {}).get("version")
        schema = tuzkam_options_schema(spec)
        domains = {
            d: DomainInfo(outputs=_OUTPUTS, options_schema=schema) for d in _configured_domains()
        }
        return EngineInfo(domains=domains, version=str(version) if version is not None else None)

    async def version(self) -> str | None:
        """The engine's reported version (``info.version`` from its OpenAPI schema)."""
        spec = await self._openapi()
        version = spec.get("info", {}).get("version") if spec else None
        return str(version) if version is not None else None

    async def submit(
        self,
        image: bytes,
        filename: str,
        domain: str | None,
        outputs: frozenset[str],
        options: dict[str, Any],
    ) -> str:
        # TuzkaM renders every output from one result, so neither the domain nor the
        # outputs go to the engine: only the image and the options.
        data = {key: form_value(value) for key, value in options.items() if value is not None}
        resp = await self._http.post(
            f"{self._url}/api/v1/process",
            headers=self._headers(),
            files={"image": (filename, image)},
            data=data,
        )
        if resp.status_code == 503:
            raise EngineFullError(f"Engine at {self._url} is full")
        resp.raise_for_status()
        job_id: str = resp.json()["job_id"]
        return job_id

    async def status(self, engine_job_id: str) -> EngineStatus:
        resp = await self._http.get(
            f"{self._url}/api/v1/status/{engine_job_id}",
            headers=self._headers(),
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return EngineStatus(
            state=EngineState.parse(data.get("status")),
            error=data.get("error"),
            error_status=data.get("error_status"),
            created_at=data.get("created_at"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            metrics=tuzkam_metrics(data),
        )

    async def fetch(self, engine_job_id: str, output: str) -> Result:
        resp = await self._http.get(
            f"{self._url}/api/v1/result/{engine_job_id}",
            headers=self._headers(),
            params={"fmt": output},
        )
        if resp.status_code == 409:
            raise OutputUnavailable(output, _detail(resp))
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "application/octet-stream")
        return Result(content=resp.content, content_type=content_type)

    # ---- internals ----

    def _headers(self) -> dict[str, str]:
        return {"X-API-Key": self._api_key} if self._api_key else {}

    async def _openapi(self) -> dict[str, Any] | None:
        """The engine's /openapi.json, cached for a short TTL (a failure too), so neither
        dispatch nor domain sync fetches it per call."""
        if time.time() - self._openapi_cached_at < _OPENAPI_CACHE_TTL_SECONDS:
            return self._openapi_cache
        spec: dict[str, Any] | None = None
        try:
            resp = await self._http.get(f"{self._url}/openapi.json", timeout=5.0)
            resp.raise_for_status()
            body = resp.json()
            spec = body if isinstance(body, dict) else None
        except Exception as exc:
            logger.warning("openapi lookup failed for %s: %s", self._url, exc)
        self._openapi_cache = spec
        self._openapi_cached_at = time.time()
        return spec


def _detail(resp: httpx.Response) -> str:
    """FastAPI's ``{"detail": ...}`` message, or the raw body."""
    try:
        detail = resp.json().get("detail")
    except (ValueError, AttributeError):
        detail = None
    return str(detail) if detail else resp.text or f"HTTP {resp.status_code}"
