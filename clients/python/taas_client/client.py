from __future__ import annotations

import asyncio
import json
import tempfile
import threading
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


class TaasClient:
    """taas client for both modes: per-page jobs (``submit``; results arrive over the
    WebSocket and go to ``on_result``/``on_error``) and batches (``submit_batch``; one
    archive in, one archive of results out, progress to ``on_batch``).

    ``domain``, ``outputs`` and ``options`` set client-wide defaults; every submit can
    override them. ``outputs`` (e.g. ``["alto", "txt"]``) replaces ``fmt`` when given.
    """

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
        self._http = httpx.Client(timeout=60.0, headers={"X-API-Key": api_key})
        # Created inside the WebSocket loop (it must live on that thread's event loop)
        # and reused for every presigned result fetch; see _ws_listen.
        self._fetch_http: httpx.AsyncClient | None = None
        self._pending: set[UUID] = set()
        # Jobs whose result arrived before their submit returned (insertion-ordered, capped).
        self._finished_early: dict[UUID, None] = {}
        self._pending_lock = threading.Lock()
        self._all_done = threading.Event()
        # Batches someone is waiting on (wait_batch), woken by a finished progress event.
        self._batch_waiters: dict[UUID, threading.Event] = {}
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

    # ---- discovery ----

    def domains(self) -> list[DomainInfo]:
        """The domains you can submit to, with their outputs and options schema."""
        resp = self._http.get(f"{self._url}/api/v1/domains")
        resp.raise_for_status()
        return _common.parse_domains(resp.json())

    # ---- per-page mode ----

    def submit(
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

        resp = self._http.post(f"{self._url}/api/v1/jobs", files=files, data=data)
        resp.raise_for_status()
        job_id = UUID(resp.json()["job_id"])

        with self._pending_lock:
            # A fast page can finish before its submit response arrives.
            if self._finished_early.pop(job_id, None) is None:
                self._pending.add(job_id)
                self._all_done.clear()
        return job_id

    def wait(self, timeout: float | None = None) -> bool:
        if not self._pending:
            return True
        return self._all_done.wait(timeout=timeout)

    # ---- batch mode ----

    def submit_batch(
        self,
        source: Path | str | bytes | Iterable[Path | str],
        external_id: str | None = None,
        domain: str | None = None,
        outputs: Iterable[str] | None = None,
        options: Mapping[str, Any] | None = None,
    ) -> BatchSubmit:
        """Submit many pages as one batch.

        ``source`` is an archive (``.tar.zst``, ``.tar.gz``, ``.tar``, ``.zip``; a path
        or its bytes), a directory, or a list of image paths. A directory or list is
        packed into a temporary ``.tar.zst`` first; nothing is held in memory whole.
        """
        data = _common.batch_form(
            external_id,
            self._fmt,
            domain or self._domain,
            outputs if outputs is not None else self._outputs,
            options if options is not None else self._options,
        )
        if isinstance(source, bytes):
            return self._post_batch(("batch.tar.zst", source), data)
        if isinstance(source, (Path, str)) and Path(source).is_file():
            path = Path(source)
            if not _common.is_archive(path):
                raise ValueError(f"{path} is not a tar, tar.gz, tar.zst or zip archive")
            with path.open("rb") as f:
                return self._post_batch((path.name, f), data)

        images = _common.collect_images(Path(source) if isinstance(source, (Path, str)) else source)
        if not images:
            raise ValueError("no images to submit")
        with tempfile.TemporaryDirectory(prefix="taas-batch-") as tmp:
            archive = Path(tmp) / "batch.tar.zst"
            _common.pack_tar_zst(images, archive)
            with archive.open("rb") as f:
                return self._post_batch((archive.name, f), data)

    def _post_batch(self, archive: tuple[str, Any], data: dict[str, str]) -> BatchSubmit:
        # No read timeout: the server unpacks and stores every page before it answers.
        resp = self._http.post(
            f"{self._url}/api/v1/batches",
            files={"archive": archive},
            data=data,
            timeout=httpx.Timeout(60.0, read=None),
        )
        resp.raise_for_status()
        return _common.parse_batch_submit(resp.json())

    def get_batch(self, batch_id: UUID, pages: bool = False) -> BatchStatus:
        """The batch's progress; ``pages=True`` adds every page's status."""
        resp = self._http.get(
            f"{self._url}/api/v1/batches/{batch_id}", params={"pages": 1} if pages else None
        )
        resp.raise_for_status()
        return _common.parse_batch_status(resp.json())

    def wait_batch(
        self, batch_id: UUID, timeout: float | None = None, poll_interval: float = 5.0
    ) -> BatchStatus:
        """Block until the batch finishes, then return its final status. Woken early by
        the WebSocket progress events once ``start()`` was called; polls otherwise.
        Raises ``TimeoutError`` if ``timeout`` passes first."""
        deadline = None if timeout is None else time.monotonic() + timeout
        waiter = self._batch_waiters.setdefault(batch_id, threading.Event())
        try:
            while True:
                status = self.get_batch(batch_id)
                if status.finished:
                    return status
                wait_for = poll_interval
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise TimeoutError(f"batch {batch_id} not finished after {timeout}s")
                    wait_for = min(wait_for, remaining)
                waiter.wait(wait_for)
                waiter.clear()
        finally:
            self._batch_waiters.pop(batch_id, None)

    def download_batch_results(
        self,
        batch_id: UUID,
        dest: Path | str,
        extract: bool = False,
        timeout: float | None = None,
        poll_interval: float = 5.0,
    ) -> BatchResults:
        """Download the batch's results archive into the ``dest`` directory, waiting
        while the server builds it. With ``extract``, also unpack it into
        ``dest/<batch_id>/`` and parse its ``manifest.json``."""
        deadline = None if timeout is None else time.monotonic() + timeout
        url = f"{self._url}/api/v1/batches/{batch_id}/results"
        while True:
            resp = self._http.get(url)
            if resp.status_code != 202:
                break
            if deadline is not None and time.monotonic() + poll_interval > deadline:
                raise TimeoutError(f"batch {batch_id} results not ready after {timeout}s")
            time.sleep(poll_interval)
        resp.raise_for_status()

        archive, directory = _common.batch_paths(dest, batch_id)
        # The presigned URL carries its own auth; don't send the API key to storage.
        with httpx.stream("GET", resp.json()["url"], timeout=60.0) as download:
            download.raise_for_status()
            with archive.open("wb") as f:
                for chunk in download.iter_bytes():
                    f.write(chunk)

        if not extract:
            return BatchResults(archive=archive)
        manifest = _common.extract_tar_zst(archive, directory)
        return BatchResults(archive=archive, directory=directory, manifest=manifest)

    # ---- WebSocket ----

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

        # One client for every presigned result fetch, for the lifetime of the socket.
        # Opening one per artifact pays a fresh TCP (and, over https, TLS) handshake per
        # result — on a high-latency link that handshake costs more than the download.
        async with httpx.AsyncClient(timeout=60.0) as fetch_http:
            self._fetch_http = fetch_http
            try:
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
            finally:
                self._fetch_http = None

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
            self._resolve(job_id)
            return

        if event["status"] == "done":
            result = JobResult(job_id=job_id, external_id=external_id)

            if event.get("alto_url"):
                alto_compressed = await self._fetch_url(event["alto_url"])
                result.alto = self._dctx.decompress(alto_compressed)

            if event.get("txt_url"):
                txt_compressed = await self._fetch_url(event["txt_url"])
                result.txt = self._dctx.decompress(txt_compressed)

            self._on_result(result)
            self._resolve(job_id)

    async def _fetch_url(self, url: str) -> bytes:
        client = self._fetch_http
        if client is None:
            raise RuntimeError("result fetch attempted outside the WebSocket loop")

        resp = await client.get(url)
        resp.raise_for_status()
        return resp.content

    def _resolve(self, job_id: UUID) -> None:
        with self._pending_lock:
            if job_id in self._pending:
                self._pending.discard(job_id)
                if not self._pending:
                    self._all_done.set()
                return
            # Not (yet) known: remember it in case its submit is still in flight.
            self._finished_early[job_id] = None
            if len(self._finished_early) > _FINISHED_EARLY_MAX:
                del self._finished_early[next(iter(self._finished_early))]
