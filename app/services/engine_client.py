import httpx


class EngineFullError(Exception):
    pass


class EngineClient:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=60.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def process(
        self,
        url: str,
        api_key: str | None,
        image_bytes: bytes,
        filename: str,
        fmt: str = "multi",
        domain: str | None = None,
        height_scale: float | None = None,
    ) -> str:
        headers = {}
        if api_key:
            headers["X-API-Key"] = api_key

        files = {"image": (filename, image_bytes)}
        data: dict[str, str] = {"fmt": fmt}
        if domain:
            data["domain"] = domain
        if height_scale is not None:
            data["height_scale"] = str(height_scale)

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
        self, url: str, api_key: str | None, engine_job_id: str, fmt: str | None = None
    ) -> bytes:
        headers = {}
        if api_key:
            headers["X-API-Key"] = api_key

        params = {}
        if fmt:
            params["fmt"] = fmt

        resp = await self._client.get(
            f"{url}/api/v1/result/{engine_job_id}",
            headers=headers,
            params=params,
        )
        resp.raise_for_status()
        return resp.content

    async def healthcheck(self, url: str) -> bool:
        try:
            resp = await self._client.get(f"{url}/healthz", timeout=5.0)
            return resp.status_code == 200
        except Exception:
            return False
