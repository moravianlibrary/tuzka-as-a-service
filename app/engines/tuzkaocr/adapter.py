"""TuzkaOCR engine adapter (text OCR)."""

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
from app.engines.openapi import form_value, options_schema_from_openapi, spec_version
from app.engines.schemas import JobAccepted
from app.engines.tuzkaocr.schemas import ModelsBody, TuzkaOcrStatus
from app.vocabulary import DEFAULT_DOMAIN, Fmt

logger = logging.getLogger(__name__)

_OPENAPI_CACHE_TTL_SECONDS = 300.0

# Outputs assumed for a domain when the engine doesn't advertise `capabilities`.
_DEFAULT_OUTPUTS = frozenset({"alto", "txt"})


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

    @classmethod
    def validate_request(
        cls, domain: str, outputs: frozenset[str], options: dict[str, Any]
    ) -> str | None:
        """Cross-field checks the options schema can't express; TuzkaOCR has none."""
        return None

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
        options_schema = options_schema_from_openapi(spec) if spec else None

        try:
            headers = {}
            if self._api_key:
                headers["X-API-Key"] = self._api_key
            resp = await self._http.get(f"{self._url}/api/v1/models", headers=headers, timeout=5.0)
            resp.raise_for_status()
            models = ModelsBody.model_validate_json(resp.content)
            capabilities = models.capabilities or {}
            domains: dict[str, DomainInfo] = {}
            for d in models.selectable_via_domain:
                if not d:
                    continue
                advertised = capabilities.get(d)
                outputs = frozenset(advertised) if advertised else _DEFAULT_OUTPUTS
                domains[d] = DomainInfo(outputs=outputs, options_schema=options_schema)
        except Exception as exc:
            logger.warning("describe failed for %s: %s", self._url, exc)
            domains = {}

        return EngineInfo(domains=domains, version=spec_version(spec))

    async def version(self) -> str | None:
        """The engine's reported version (``info.version`` from its OpenAPI schema)."""
        return spec_version(await self._openapi())

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
        data: dict[str, str] = {"fmt": Fmt.from_outputs(outputs)}
        # "default" is the engine's no-domain request; sending it would change nothing
        # but the request shape (D6).
        if domain and domain != DEFAULT_DOMAIN:
            data["domain"] = domain
        for key, value in options.items():
            if value is not None:
                data[key] = form_value(value)

        resp = await self._http.post(
            f"{self._url}/api/v1/process",
            headers=headers,
            files=files,
            data=data,
        )
        if resp.status_code == 503:
            raise EngineFullError(f"Engine at {self._url} is full")
        resp.raise_for_status()
        return JobAccepted.model_validate_json(resp.content).job_id

    async def status(self, engine_job_id: str) -> EngineStatus:
        headers = {}
        if self._api_key:
            headers["X-API-Key"] = self._api_key

        resp = await self._http.get(
            f"{self._url}/api/v1/status/{engine_job_id}",
            headers=headers,
        )
        resp.raise_for_status()
        body = TuzkaOcrStatus.model_validate_json(resp.content)
        return body.engine_status(body.metrics())

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
