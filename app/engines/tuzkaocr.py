"""TuzkaOCR engine adapter — the only implementation today."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx

from app.engines.base import (
    DomainInfo,
    EngineFullError,
    EngineInfo,
    EngineStatus,
    Result,
)
from app.engines.outputs import outputs_to_fmt

logger = logging.getLogger(__name__)

_OPENAPI_CACHE_TTL_SECONDS = 300.0

# Outputs assumed for a domain when the engine doesn't advertise `capabilities`.
_DEFAULT_OUTPUTS = frozenset({"alto", "txt"})

# Form fields of POST /api/v1/process that taas sets itself; the rest are options.
_RESERVED_FIELDS = frozenset({"image", "domain", "fmt"})


def options_schema_from_openapi(spec: dict[str, Any]) -> dict[str, Any] | None:
    """Derive the per-job options JSON Schema from the engine's OpenAPI document.

    Takes the multipart form body of ``POST /api/v1/process``, drops the fields taas
    owns (image, domain, fmt) and closes the object, so an unknown option is rejected
    at submit instead of being silently ignored by the engine. Returns None when the
    document doesn't describe that body.
    """
    try:
        body = spec["paths"]["/api/v1/process"]["post"]["requestBody"]
        schema: dict[str, Any] = body["content"]["multipart/form-data"]["schema"]
        ref = schema.get("$ref")
        if ref is not None:
            # FastAPI emits a local ref: #/components/schemas/Body_process_image_...
            name = ref.removeprefix("#/components/schemas/")
            schema = spec["components"]["schemas"][name]
        properties: dict[str, Any] = schema.get("properties", {})
    except (KeyError, TypeError, AttributeError):
        return None

    options: dict[str, Any] = {
        "type": "object",
        "properties": {k: v for k, v in properties.items() if k not in _RESERVED_FIELDS},
        "additionalProperties": False,
    }
    required = [r for r in schema.get("required", []) if r not in _RESERVED_FIELDS]
    if required:
        options["required"] = required
    return options


def _form_value(value: Any) -> str:
    """Render an option as a form field; booleans the way FastAPI parses them."""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


class TuzkaOcrAdapter:
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

    async def healthy(self) -> bool:
        try:
            resp = await self._http.get(f"{self._url}/healthz", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            logger.warning("healthcheck failed for %s", self._url, exc_info=True)
            return False

    async def describe(self) -> EngineInfo:
        """Return the engine's domains (with outputs and options schema) and version.

        Outputs come from the optional ``capabilities`` key of /api/v1/models; a domain
        it doesn't list gets {alto, txt}. TuzkaOCR has a single process endpoint, so
        every domain shares the one options schema derived from its OpenAPI.
        """
        spec = await self._openapi()
        version = spec.get("info", {}).get("version") if spec else None
        options_schema = options_schema_from_openapi(spec) if spec else None

        try:
            headers = {}
            if self._api_key:
                headers["X-API-Key"] = self._api_key
            resp = await self._http.get(f"{self._url}/api/v1/models", headers=headers, timeout=5.0)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            domains_data = data.get("selectable_via_domain", [])
            capabilities: dict[str, Any] = data.get("capabilities") or {}
            domains: dict[str, DomainInfo] = {}
            for d in domains_data:
                if not d:
                    continue
                advertised = capabilities.get(d)
                outputs = frozenset(str(o) for o in advertised) if advertised else _DEFAULT_OUTPUTS
                domains[str(d)] = DomainInfo(outputs=outputs, options_schema=options_schema)
        except Exception as exc:
            logger.warning("describe failed for %s: %s", self._url, exc)
            domains = {}

        return EngineInfo(domains=domains, version=version)

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
        headers = {}
        if self._api_key:
            headers["X-API-Key"] = self._api_key

        files = {"image": (filename, image)}
        data: dict[str, str] = {"fmt": outputs_to_fmt(outputs)}
        # "default" is the engine's no-domain request; sending it would change nothing
        # but the request shape (D6).
        if domain and domain != "default":
            data["domain"] = domain
        for key, value in options.items():
            if value is not None:
                data[key] = _form_value(value)

        resp = await self._http.post(
            f"{self._url}/api/v1/process",
            headers=headers,
            files=files,
            data=data,
        )
        if resp.status_code == 503:
            raise EngineFullError(f"Engine at {self._url} is full")
        resp.raise_for_status()
        job_id: str = resp.json()["job_id"]
        return job_id

    async def status(self, engine_job_id: str) -> EngineStatus:
        headers = {}
        if self._api_key:
            headers["X-API-Key"] = self._api_key

        resp = await self._http.get(
            f"{self._url}/api/v1/status/{engine_job_id}",
            headers=headers,
        )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        metrics: dict[str, int | float] = {}
        for key in ("n_lines", "mean_conf"):
            val = data.get(key)
            if val is not None and isinstance(val, (int, float)):
                metrics[key] = val
        return EngineStatus(
            state=data.get("status", "unknown"),
            error=data.get("error"),
            error_status=data.get("error_status"),
            created_at=data.get("created_at"),
            started_at=data.get("started_at"),
            finished_at=data.get("finished_at"),
            metrics=metrics,
        )

    async def fetch(self, engine_job_id: str, output: str) -> Result:
        headers = {}
        if self._api_key:
            headers["X-API-Key"] = self._api_key

        params = {"which": output}

        resp = await self._http.get(
            f"{self._url}/api/v1/result/{engine_job_id}",
            headers=headers,
            params=params,
        )
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "application/octet-stream")
        return Result(content=resp.content, content_type=content_type)

    # ---- internals ----

    async def _openapi(self) -> dict[str, Any] | None:
        """The engine's /openapi.json, cached for a short TTL so dispatch doesn't fetch
        it per job. Best-effort: a failed lookup is cached as ``None`` too, so dispatch
        never fails or stalls on it.
        """
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
