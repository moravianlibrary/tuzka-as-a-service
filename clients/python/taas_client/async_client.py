from __future__ import annotations

import asyncio
import json
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import websockets
import zstandard

from . import _common
from .models import (
    BatchProgress,
    BatchResults,
    BatchStatus,
    BatchSubmit,
    DomainInfo,
    JobEvent,
    JobResult,
    JobStatus,
)

# Job ids remembered from results that arrived before their submit response returned.
_FINISHED_EARLY_MAX = 10_000


class AsyncTaasClient:
    """The asyncio twin of ``TaasClient``: the same per-page and batch modes, with
    coroutine methods. Archive packing and unpacking run in a worker thread."""

    def __init__(
        self,
        url: str,
        api_key: str,
        on_result: Callable[[JobResult], None],
        on_error: Callable[[JobEvent], None],
        fmt: str = "multi",
        domain: str | None = None,
        outputs: Iterable[str] | None = None,
        options: Mapping[str, Any] | None = None,
        on_batch: Callable[[BatchProgress], None] | None = None,
    ):
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._on_result = on_result
        self._on_error = on_error
        self._on_batch = on_batch
        self._fmt = fmt
        self._domain = domain
        self._outputs = list(outputs) if outputs is not None else None
        self._options = dict(options) if options else None
        self._http = httpx.AsyncClient(timeout=60.0, headers={"X-API-Key": api_key})
        # Presigned result URLs carry their own auth; this client sends no API key.
        self._fetch_http = httpx.AsyncClient(timeout=60.0)
        self._pending: set[UUID] = set()
        # Jobs whose result arrived before their submit returned (insertion-ordered, capped).
        self._finished_early: dict[UUID, None] = {}
        self._lock = asyncio.Lock()
        self._all_done = asyncio.Event()
        self._batch_waiters: dict[UUID, asyncio.Event] = {}
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
        await self._fetch_http.aclose()

    # ---- discovery ----

    async def domains(self) -> list[DomainInfo]:
        """The domains you can submit to, with their outputs and options schema."""
        resp = await self._http.get(f"{self._url}/api/v1/domains")
        resp.raise_for_status()
        return _common.parse_domains(resp.json())

    # ---- per-page mode ----

    async def submit(
        self,
        image: Path | str | bytes,
        external_id: str | None = None,
        fmt: str | None = None,
        domain: str | None = None,
        outputs: Iterable[str] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> UUID:
        """Submit one page and return its job id. ``external_id`` is your own label for
        the page (any string up to 255 characters, e.g. its PID); it comes back on the
        page's ``JobResult``/``JobEvent``."""
        if isinstance(image, (Path, str)):
            path = Path(image)
            image_bytes = path.read_bytes()
            filename = path.name
        else:
            image_bytes = image
            filename = "image"

        files = {"image": (filename, image_bytes)}
        data = _common.job_form(
            external_id,
            fmt or self._fmt,
            domain or self._domain,
            outputs if outputs is not None else self._outputs,
            options if options is not None else self._options,
        )

        resp = await self._http.post(f"{self._url}/api/v1/jobs", files=files, data=data)
        resp.raise_for_status()
        job_id = UUID(resp.json()["job_id"])

        async with self._lock:
            # A fast page can finish before its submit response arrives.
            if self._finished_early.pop(job_id, None) is None:
                self._pending.add(job_id)
                self._all_done.clear()
        return job_id

    async def wait(self, timeout: float | None = None) -> bool:
        if not self._pending:
            return True
        try:
            await asyncio.wait_for(self._all_done.wait(), timeout=timeout)
            return True
        except TimeoutError:
            return False

    # ---- batch mode ----

    async def submit_batch(
        self,
        source: Path | str | bytes | Iterable[Path | str],
        external_id: str | None = None,
        domain: str | None = None,
        outputs: Iterable[str] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> BatchSubmit:
        """Submit many pages as one batch; see ``TaasClient.submit_batch``."""
        data = _common.batch_form(
            external_id,
            self._fmt,
            domain or self._domain,
            outputs if outputs is not None else self._outputs,
            options if options is not None else self._options,
        )
        if isinstance(source, bytes):
            return await self._post_batch(("batch.tar.zst", source), data)
        if isinstance(source, (Path, str)) and Path(source).is_file():
            path = Path(source)
            if not await asyncio.to_thread(_common.is_archive, path):
                raise ValueError(f"{path} is not a tar, tar.gz, tar.zst or zip archive")
            with path.open("rb") as f:
                return await self._post_batch((path.name, f), data)

        images = _common.collect_images(Path(source) if isinstance(source, (Path, str)) else source)
        if not images:
            raise ValueError("no images to submit")
        with tempfile.TemporaryDirectory(prefix="taas-batch-") as tmp:
            archive = Path(tmp) / "batch.tar.zst"
            await asyncio.to_thread(_common.pack_tar_zst, images, archive)
            with archive.open("rb") as f:
                return await self._post_batch((archive.name, f), data)

    async def _post_batch(self, archive: tuple[str, Any], data: dict[str, str]) -> BatchSubmit:
        # No read timeout: the server unpacks and stores every page before it answers.
        resp = await self._http.post(
            f"{self._url}/api/v1/batches",
            files={"archive": archive},
            data=data,
            timeout=httpx.Timeout(60.0, read=None),
        )
        resp.raise_for_status()
        return _common.parse_batch_submit(resp.json())

    async def get_batch(self, batch_id: UUID, pages: bool = False) -> BatchStatus:
        """The batch's progress; ``pages=True`` adds every page's status."""
        resp = await self._http.get(
            f"{self._url}/api/v1/batches/{batch_id}", params={"pages": 1} if pages else None
        )
        resp.raise_for_status()
        return _common.parse_batch_status(resp.json())

    async def wait_batch(
        self, batch_id: UUID, timeout: float | None = None, poll_interval: float = 5.0
    ) -> BatchStatus:
        """Wait until the batch finishes, then return its final status; see
        ``TaasClient.wait_batch``."""
        deadline = None if timeout is None else time.monotonic() + timeout
        waiter = self._batch_waiters.setdefault(batch_id, asyncio.Event())
        try:
            while True:
                status = await self.get_batch(batch_id)
                if status.finished:
                    return status
                wait_for = poll_interval
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"batch {batch_id} not finished after {timeout}s")
                    wait_for = min(wait_for, remaining)
                try:
                    await asyncio.wait_for(waiter.wait(), timeout=wait_for)
                except TimeoutError:
                    pass
                waiter.clear()
        finally:
            self._batch_waiters.pop(batch_id, None)

    async def download_batch_results(
        self,
        batch_id: UUID,
        dest: Path | str,
        extract: bool = False,
        timeout: float | None = None,
        poll_interval: float = 5.0,
    ) -> BatchResults:
        """Download (and optionally extract) the batch's results archive; see
        ``TaasClient.download_batch_results``."""
        deadline = None if timeout is None else time.monotonic() + timeout
        url = f"{self._url}/api/v1/batches/{batch_id}/results"
        while True:
            resp = await self._http.get(url)
            if resp.status_code != 202:
                break
            if deadline is not None and time.monotonic() + poll_interval > deadline:
                raise TimeoutError(f"batch {batch_id} results not ready after {timeout}s")
            await asyncio.sleep(poll_interval)
        resp.raise_for_status()

        archive, directory = _common.batch_paths(dest, batch_id)
        async with self._fetch_http.stream("GET", resp.json()["url"]) as download:
            download.raise_for_status()
            with archive.open("wb") as f:
                async for chunk in download.aiter_bytes():
                    f.write(chunk)

        if not extract:
            return BatchResults(archive=archive)
        manifest = await asyncio.to_thread(_common.extract_tar_zst, archive, directory)
        return BatchResults(archive=archive, directory=directory, manifest=manifest)

    # ---- WebSocket ----

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

    async def _handle_event(self, raw: str | bytes) -> None:
        event = json.loads(raw)

        progress = _common.parse_batch_event(event)
        if progress is not None:
            if self._on_batch is not None:
                self._on_batch(progress)
            waiter = self._batch_waiters.get(progress.batch_id)
            if waiter is not None and progress.finished:
                waiter.set()
            return
        if not _common.is_page_event(event):
            return  # a batch page, or an event this client doesn't know

        job_id = UUID(event["job_id"])
        external_id = event.get("external_id")

        if event["status"] == "failed":
            job_event = JobEvent(
                job_id=job_id,
                status=JobStatus.FAILED,
                external_id=external_id,
                error=event.get("error"),
                ts=event.get("ts"),
            )
            self._on_error(job_event)
            await self._resolve(job_id)
            return

        if event["status"] == "done":
            result = JobResult(job_id=job_id, external_id=external_id)

            if event.get("alto_url"):
                resp = await self._fetch_http.get(event["alto_url"])
                resp.raise_for_status()
                result.alto = self._dctx.decompress(resp.content)

            if event.get("txt_url"):
                resp = await self._fetch_http.get(event["txt_url"])
                resp.raise_for_status()
                result.txt = self._dctx.decompress(resp.content)

            self._on_result(result)
            await self._resolve(job_id)

    async def _resolve(self, job_id: UUID) -> None:
        async with self._lock:
            if job_id in self._pending:
                self._pending.discard(job_id)
                if not self._pending:
                    self._all_done.set()
                return
            # Not (yet) known: remember it in case its submit is still in flight.
            self._finished_early[job_id] = None
            if len(self._finished_early) > _FINISHED_EARLY_MAX:
                del self._finished_early[next(iter(self._finished_early))]
