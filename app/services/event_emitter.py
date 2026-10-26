"""Fire-and-forget shipping of job events to ClickHouse (step 5, D3).

``emit`` only puts the row on an in-process queue; a background task posts batches
(every 2 s or 500 rows) as ``INSERT … FORMAT JSONEachRow`` over ClickHouse's HTTP
interface. A full queue or a failed insert drops the rows with a rate-limited warning:
**a job never fails, or waits, because analytics did.**
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from typing import Any

import httpx

from app.config import Settings
from app.services.events import JobEvent, job_event_row

logger = logging.getLogger(__name__)

QUEUE_MAX = 10_000
BATCH_ROWS = 500
FLUSH_SECONDS = 2.0
CLOSE_TIMEOUT_SECONDS = 5.0
_DROP_WARN_EVERY_SECONDS = 60.0


class EventEmitter:
    def __init__(
        self,
        url: str,
        user: str = "default",
        password: str = "",
        database: str = "taas",
        http: httpx.AsyncClient | None = None,
        queue_max: int = QUEUE_MAX,
    ) -> None:
        self.enabled = bool(url)
        self._url = url.rstrip("/")
        self._auth = (user, password)
        self._query = f"INSERT INTO {database}.job_events FORMAT JSONEachRow"
        self._http = http
        self._owns_http = http is None
        self._queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=queue_max)
        self._task: asyncio.Task[None] | None = None
        # Rows the background task has taken off the queue but not posted yet; kept
        # here so a cancel mid-batch (aclose) doesn't lose them.
        self._batch: list[dict[str, Any]] = []
        self._dropped = 0
        self._last_warned = 0.0

    async def start(self) -> None:
        if not self.enabled or self._task is not None:
            return
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=10.0)
        self._task = asyncio.create_task(self._run())

    def emit(self, event: JobEvent) -> None:
        """Queue one event; never blocks, never raises."""
        if not self.enabled:
            return
        try:
            self._queue.put_nowait(job_event_row(event))
        except asyncio.QueueFull:
            self._drop(1, "queue full")

    async def flush(self) -> None:
        """Post everything queued now, in batches."""
        if self._batch:
            batch, self._batch = self._batch, []
            await self._post(batch)
        while not self._queue.empty():
            await self._post(self._take(BATCH_ROWS))

    async def aclose(self) -> None:
        """Stop the background task and post what's left, giving up after 5 s."""
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.enabled:
            try:
                await asyncio.wait_for(self.flush(), CLOSE_TIMEOUT_SECONDS)
            except TimeoutError:
                self._drop(self._queue.qsize(), "shutdown timeout")
        if self._owns_http and self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _run(self) -> None:
        while True:
            # Wait for the first row, then give the batch up to FLUSH_SECONDS to fill.
            self._batch.append(await self._queue.get())
            deadline = time.monotonic() + FLUSH_SECONDS
            while len(self._batch) < BATCH_ROWS:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    self._batch.append(await asyncio.wait_for(self._queue.get(), remaining))
                except TimeoutError:
                    break
            batch, self._batch = self._batch, []
            await self._post(batch)

    def _take(self, n: int) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        while len(rows) < n and not self._queue.empty():
            rows.append(self._queue.get_nowait())
        return rows

    async def _post(self, rows: list[dict[str, Any]]) -> None:
        if not rows or self._http is None:
            return
        body = "\n".join(json.dumps(row) for row in rows)
        try:
            resp = await self._http.post(
                f"{self._url}/", params={"query": self._query}, content=body, auth=self._auth
            )
            resp.raise_for_status()
        except Exception as exc:
            self._drop(len(rows), f"insert failed: {exc}")

    def _drop(self, n: int, reason: str) -> None:
        self._dropped += n
        now = time.monotonic()
        if now - self._last_warned >= _DROP_WARN_EVERY_SECONDS:
            logger.warning("Dropped %d job event(s) so far (latest: %s)", self._dropped, reason)
            self._last_warned = now


_emitter: EventEmitter | None = None


def get_emitter(settings: Settings | None = None) -> EventEmitter:
    """The process's emitter (created on first use; a no-op without ClickHouse)."""
    global _emitter
    if _emitter is None:
        s = settings or Settings()
        _emitter = EventEmitter(
            s.clickhouse_url, s.clickhouse_user, s.clickhouse_password, s.clickhouse_database
        )
    return _emitter
