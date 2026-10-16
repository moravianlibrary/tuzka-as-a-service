"""TuzkaOCR engine adapter — the only implementation today."""

from __future__ import annotations

import logging
import time
from typing import Any

import httpx
from pydantic import BaseModel

from app.engines.base import (
    DomainInfo,
    EngineFullError,
    EngineInfo,
    EngineStatus,
    Result,
)
from app.engines.outputs import outputs_to_fmt

logger = logging.getLogger(__name__)

_VERSION_CACHE_TTL_SECONDS = 300.0


class NoOptions(BaseModel):
    """Placeholder for domains that have no submit-time options."""


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
        self._version_cache: str | None = None
        self._version_cached_at: float = 0.0

    # ---- public ----

    async def healthy(self) -> bool:
        try:
            resp = await self._http.get(f"{self._url}/healthz", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            logger.warning("healthcheck failed for %s", self._url, exc_info=True)
            return False

    async def describe(self) -> EngineInfo:
        """Return the engine's advertised domains and version."""
        # Fetch version from OpenAPI (caches internally)
        await self._fetch_version()

        try:
            headers = {}
            if self._api_key:
                headers["X-API-Key"] = self._api_key
            resp = await self._http.get(f"{self._url}/api/v1/models", headers=headers, timeout=5.0)
            resp.raise_for_status()
            data: dict[str, Any] = resp.json()
            domains_data = data.get("selectable_via_domain", [])
            domains: dict[str, DomainInfo] = {
                str(d): DomainInfo(outputs=frozenset({"alto", "txt"})) for d in domains_data if d
            }
        except Exception as exc:
            logger.warning("describe failed for %s: %s", self._url, exc)
            domains = {}

        return EngineInfo(domains=domains, version=self._version_cache)

    async def _fetch_version(self) -> None:
        """Fetch engine version from OpenAPI, cached per instance."""
        if (
            self._version_cache is not None
            and time.time() - self._version_cached_at < _VERSION_CACHE_TTL_SECONDS
        ):
            return
        version: str | None = None
        try:
            resp = await self._http.get(f"{self._url}/openapi.json", timeout=5.0)
            version = resp.json().get("info", {}).get("version")
        except Exception as exc:
            logger.warning("version lookup failed for %s: %s", self._url, exc)
        self._version_cache = version
        self._version_cached_at = time.time()

    async def submit(
        self,
        image: bytes,
        filename: str,
        domain: str | None,
        outputs: frozenset[str],
        options: BaseModel,
    ) -> str:
        headers = {}
        if self._api_key:
            headers["X-API-Key"] = self._api_key

        files = {"image": (filename, image)}
        data: dict[str, str] = {"fmt": outputs_to_fmt(outputs)}
        if domain:
            data["domain"] = domain

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

    def options_schema(self, domain: str) -> type[BaseModel]:
        return NoOptions

    def version(self) -> str | None:
        """Return the cached engine version, or None if not yet fetched."""
        return self._version_cache
