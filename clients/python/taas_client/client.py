from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import websockets
import zstandard

from .models import JobEvent, JobResult, JobStatus


class TaasClient:
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
        self._http = httpx.Client(timeout=60.0)
        self._pending: set[UUID] = set()
        self._pending_lock = threading.Lock()
        self._all_done = threading.Event()
        self._ws_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._dctx = zstandard.ZstdDecompressor()

    def start(self) -> None:
        self._stop_event.clear()
        self._all_done.clear()
        self._ws_thread = threading.Thread(target=self._ws_loop, daemon=True)
        self._ws_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=5)
        self._http.close()

    def submit(
        self,
        image: Path | str | bytes,
        uuid: UUID | None = None,
        fmt: str | None = None,
        domain: str | None = None,
    ) -> UUID:
        ext_id = uuid or uuid4()

        with self._pending_lock:
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

        resp = self._http.post(
            f"{self._url}/api/v1/jobs",
            headers={"X-API-Key": self._api_key},
            files=files,
            data=data,
        )
        if resp.status_code >= 400:
            with self._pending_lock:
                self._pending.discard(ext_id)
            resp.raise_for_status()

        return ext_id

    def wait(self, timeout: float | None = None) -> bool:
        if not self._pending:
            return True
        return self._all_done.wait(timeout=timeout)

    def _ws_loop(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._ws_listen())
        finally:
            loop.close()

    async def _ws_listen(self) -> None:
        ws_url = self._url.replace("http://", "ws://").replace("https://", "wss://")
        ws_url = f"{ws_url}/ws?api_key={self._api_key}"

        while not self._stop_event.is_set():
            try:
                async with websockets.connect(ws_url) as ws:
                    async for raw in ws:
                        if self._stop_event.is_set():
                            break
                        await self._handle_event(raw)
            except websockets.ConnectionClosed:
                if self._stop_event.is_set():
                    break
                await asyncio.sleep(2)
            except Exception:
                if self._stop_event.is_set():
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
            self._resolve(ext_id)
            return

        if event["status"] == "done":
            result = JobResult(uuid=ext_id)

            if event.get("alto_url"):
                alto_compressed = await self._fetch_url(event["alto_url"])
                result.alto = self._dctx.decompress(alto_compressed)

            if event.get("txt_url"):
                txt_compressed = await self._fetch_url(event["txt_url"])
                result.txt = self._dctx.decompress(txt_compressed)

            self._on_result(result)
            self._resolve(ext_id)

    async def _fetch_url(self, url: str) -> bytes:
        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.content

    def _resolve(self, uuid: UUID) -> None:
        with self._pending_lock:
            self._pending.discard(uuid)
            if not self._pending:
                self._all_done.set()
