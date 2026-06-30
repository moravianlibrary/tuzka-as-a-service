import time

import httpx

# How long a fetched engine version is trusted before re-querying the engine.
_VERSION_CACHE_TTL_SECONDS = 300.0


class EngineFullError(Exception):
    pass


class EngineClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=60.0)
        # url -> (version, fetched_at); avoids an OpenAPI fetch on every dispatch.
        self._version_cache: dict[str, tuple[str | None, float]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def get_version(self, url: str) -> str | None:
        """Return the engine's reported version (`info.version` from its OpenAPI
        schema), cached per-url for a short TTL. Best-effort: returns ``None`` if the
        engine is unreachable or the schema lacks a version, so dispatch never fails
        on a version lookup."""
        cached = self._version_cache.get(url)
        if cached and time.time() - cached[1] < _VERSION_CACHE_TTL_SECONDS:
            return cached[0]

        version: str | None = None
        try:
            resp = await self._client.get(f"{url}/openapi.json", timeout=5.0)
            resp.raise_for_status()
            version = resp.json().get("info", {}).get("version")
        except Exception:
            version = None

        self._version_cache[url] = (version, time.time())
        return version

    async def process(
        self,
        url: str,
        api_key: str | None,
        image_bytes: bytes,
        filename: str,
        fmt: str = "multi",
        domain: str | None = None,
    ) -> str:
        headers = {}
        if api_key:
            headers["X-API-Key"] = api_key

        files = {"image": (filename, image_bytes)}
        data: dict[str, str] = {"fmt": fmt}
        if domain:
            data["domain"] = domain

        resp = await self._client.post(
            f"{url}/api/v1/process",
            headers=headers,
            files=files,
            data=data,
        )
        if resp.status_code == 503:
            raise EngineFullError(f"Engine at {url} is full")
        resp.raise_for_status()
        return resp.json()["job_id"]

    async def check_status(self, url: str, api_key: str | None, engine_job_id: str) -> dict:
        headers = {}
        if api_key:
            headers["X-API-Key"] = api_key

        resp = await self._client.get(
            f"{url}/api/v1/status/{engine_job_id}",
            headers=headers,
        )
        resp.raise_for_status()
        return resp.json()

    async def get_result(
        self, url: str, api_key: str | None, engine_job_id: str, which: str | None = None
    ) -> bytes:
        # `which` selects a single output (alto|txt) from a multi-format job;
        # omit it for single-format jobs to fetch the sole result.
        headers = {}
        if api_key:
            headers["X-API-Key"] = api_key

        params = {}
        if which:
            params["which"] = which

        resp = await self._client.get(
            f"{url}/api/v1/result/{engine_job_id}",
            headers=headers,
            params=params,
        )
        resp.raise_for_status()
        return resp.content

    async def get_models(self, url: str, api_key: str | None) -> list[str]:
        """Return the list of domain names the engine can serve, from GET /api/v1/models.

        Returns an empty list if the endpoint is unreachable or the response is malformed."""
        headers = {}
        if api_key:
            headers["X-API-Key"] = api_key
        try:
            resp = await self._client.get(f"{url}/api/v1/models", headers=headers, timeout=5.0)
            resp.raise_for_status()
            data = resp.json()
            domains = data.get("selectable_via_domain", [])
            return [str(d) for d in domains if d]
        except Exception:
            return []

    async def healthcheck(self, url: str) -> bool:
        try:
            resp = await self._client.get(f"{url}/healthz", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False
