from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import websockets
import zstandard

from .models import JobEvent, JobResult, JobStatus


class AsyncTaasClient:
    def __init__(
        self,
        url: str,
        api_key: str,
        on_result: Callable[[JobResult], None],
        on_error: Callable[[JobEvent], None],
        fmt: str = "multi",
        domain: str | None = None,
    ):
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._on_result = on_result
        self._on_error = on_error
        self._fmt = fmt
        self._domain = domain
        self._http = httpx.AsyncClient(timeout=60.0)
        self._pending: set[UUID] = set()
        self._lock = asyncio.Lock()
        self._all_done = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._stop = False
        self._dctx = zstandard.ZstdDecompressor()

    async def start(self) -> None:
        self._stop = False
        self._all_done.clear()
        self._task = asyncio.create_task(self._ws_listen())

    async def stop(self) -> None:
        self._stop = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        await self._http.aclose()

    async def submit(
        self,
        image: Path | str | bytes,
        uuid: UUID | None = None,
        fmt: str | None = None,
        domain: str | None = None,
    ) -> UUID:
        ext_id = uuid or uuid4()

        async with self._lock:
            self._pending.add(ext_id)
            self._all_done.clear()

        if isinstance(image, (Path, str)):
            path = Path(image)
            image_bytes = path.read_bytes()
            filename = path.name
        else:
            image_bytes = image
            filename = "image"

        files = {"image": (filename, image_bytes)}
        data: dict[str, str] = {
            "uuid": str(ext_id),
            "fmt": fmt or self._fmt,
        }
        if domain or self._domain:
            data["domain"] = domain or self._domain  # type: ignore

        resp = await self._http.post(
            f"{self._url}/api/v1/jobs",
            headers={"X-API-Key": self._api_key},
            files=files,
            data=data,
        )
        if resp.status_code >= 400:
            async with self._lock:
                self._pending.discard(ext_id)
            resp.raise_for_status()

        return ext_id

    async def wait(self, timeout: float | None = None) -> bool:
        if not self._pending:
            return True
        try:
            await asyncio.wait_for(self._all_done.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    async def _ws_listen(self) -> None:
        ws_url = self._url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/ws?api_key={self._api_key}"

        while not self._stop:
            try:
                async with websockets.connect(ws_url) as ws:
                    async for raw in ws:
                        if self._stop:
                            break
                        await self._handle_event(raw)
            except websockets.ConnectionClosed:
                if self._stop:
                    break
                await asyncio.sleep(2)
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._stop:
                    break
                await asyncio.sleep(5)

    async def _handle_event(self, raw: str) -> None:
        event = json.loads(raw)
        ext_id = UUID(event["uuid"])

        if event["status"] == "failed":
            job_event = JobEvent(
                uuid=ext_id,
                status=JobStatus.FAILED,
                error=event.get("error"),
                ts=event.get("ts"),
            )
            self._on_error(job_event)
            await self._resolve(ext_id)
            return

        if event["status"] == "done":
            result = JobResult(uuid=ext_id)

            if event.get("alto_url"):
                resp = await self._http.get(event["alto_url"])
                resp.raise_for_status()
                result.alto = self._dctx.decompress(resp.content)

            if event.get("txt_url"):
                resp = await self._http.get(event["txt_url"])
                resp.raise_for_status()
                result.txt = self._dctx.decompress(resp.content)

            self._on_result(result)
            await self._resolve(ext_id)

    async def _resolve(self, uuid: UUID) -> None:
        async with self._lock:
            self._pending.discard(uuid)
            if not self._pending:
                self._all_done.set()
