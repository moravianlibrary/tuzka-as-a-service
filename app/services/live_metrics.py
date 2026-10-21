"""Live queue numbers into ClickHouse's ``taas.live_metrics`` (step 5, D6).

Every 15 s the cleanup worker snapshots the load counters behind ``GET /load`` (step 4)
and each backend's in-flight count, and inserts them as rows
``(ts, metric, labels, value)``. HyperDX charts queue depth and backend use from them.
Like the job events it's fire-and-forget: a failed insert is logged (rate-limited) and
dropped, and with ``CLICKHOUSE_URL`` unset nothing runs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime
from typing import Any

import httpx
import redis.asyncio as aioredis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.clock import utcnow
from app.config import Settings
from app.models.backend import Backend
from app.services.events import ch_datetime
from app.services.load import parse_load_field
from app.services.redis_jobs import LOAD_INFLIGHT_KEY, LOAD_QUEUED_KEY

logger = logging.getLogger(__name__)

INTERVAL_SECONDS = 15.0
_WARN_EVERY_SECONDS = 60.0


def _counts(raw: dict[Any, Any]) -> dict[str, int]:
    def s(v: Any) -> str:
        return v.decode() if isinstance(v, bytes) else str(v)

    return {s(k): int(v) for k, v in raw.items()}


def metric_rows(
    ts: datetime,
    queued: dict[str, int],
    inflight: dict[str, int],
    backends: list[tuple[int, str | None, int]],
) -> list[dict[str, Any]]:
    """The rows of one snapshot. ``queued`` / ``inflight`` are the load counters
    (``domain|outputs`` → pages); ``backends`` is ``(id, label, inflight)``. A counter at
    zero is still written, so a chart drops to zero instead of keeping its last value."""
    stamp = ch_datetime(ts)
    rows: list[dict[str, Any]] = []
    for metric, counts in (("queued", queued), ("inflight", inflight)):
        for field, value in sorted(counts.items()):
            domain, outputs = parse_load_field(field)
            labels = {"domain": domain or "default", "outputs": ",".join(sorted(outputs))}
            rows.append({"ts": stamp, "metric": metric, "labels": labels, "value": value})
    for backend_id, label, value in backends:
        labels = {"backend_id": str(backend_id), "backend": label or str(backend_id)}
        rows.append({"ts": stamp, "metric": "backend_inflight", "labels": labels, "value": value})
    return rows


async def snapshot(r: aioredis.Redis, db: AsyncSession) -> list[dict[str, Any]]:
    """Read the counters now and turn them into rows."""
    queued = _counts(await r.hgetall(LOAD_QUEUED_KEY))
    inflight = _counts(await r.hgetall(LOAD_INFLIGHT_KEY))
    enabled = (
        await db.execute(select(Backend.id, Backend.label).where(Backend.enabled.is_(True)))
    ).all()
    keys = [f"backend:{backend_id}:inflight" for backend_id, _ in enabled]
    values = await r.mget(keys) if keys else []  # MGET with no keys is an error
    backends = [
        (backend_id, label, int(value or 0))
        for (backend_id, label), value in zip(enabled, values, strict=True)
    ]
    return metric_rows(utcnow(), queued, inflight, backends)


async def insert_rows(
    http: httpx.AsyncClient, settings: Settings, rows: list[dict[str, Any]]
) -> None:
    """Write a live-metrics batch through the existing ClickHouse endpoint."""
    if not rows:
        return
    body = "\n".join(json.dumps(row) for row in rows)
    response = await http.post(
        f"{settings.clickhouse_url.rstrip('/')}/",
        params={
            "query": f"INSERT INTO {settings.clickhouse_database}.live_metrics FORMAT JSONEachRow"
        },
        content=body,
        auth=(settings.clickhouse_user, settings.clickhouse_password),
    )
    response.raise_for_status()


async def run(
    r: aioredis.Redis, session_factory: async_sessionmaker[AsyncSession], settings: Settings
) -> None:
    """Insert a snapshot every ``INTERVAL_SECONDS`` until cancelled."""
    last_warned = 0.0
    async with httpx.AsyncClient(timeout=10.0) as http:
        while True:
            try:
                async with session_factory() as db:
                    rows = await snapshot(r, db)
                await insert_rows(http, settings, rows)
            except Exception as exc:
                now = time.monotonic()
                if now - last_warned >= _WARN_EVERY_SECONDS:
                    logger.warning("Live metrics snapshot dropped: %s", exc)
                    last_warned = now
            await asyncio.sleep(INTERVAL_SECONDS)
