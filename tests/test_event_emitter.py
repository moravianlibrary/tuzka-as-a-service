"""Tests for the fire-and-forget job event emitter (step 5, D3), via httpx.MockTransport."""

from datetime import datetime

import httpx
import pytest

from app.services.event_emitter import EventEmitter
from app.services.events import JobEvent


def _event(i: int = 0) -> JobEvent:
    return JobEvent(
        ts=datetime(2026, 10, 20, 12, 0, 0),
        event="done",
        job_id=f"job-{i}",
        username="alice",
        domain="default",
    )


def _emitter(handler, url: str = "http://ch:8123", queue_max: int = 10_000) -> EventEmitter:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EventEmitter(url, "taas", "pw", "taas", http=http, queue_max=queue_max)


@pytest.mark.asyncio
async def test_batches_of_500() -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(200)

    emitter = _emitter(handler)
    for i in range(600):
        emitter.emit(_event(i))
    await emitter.flush()

    assert [len(p.content.splitlines()) for p in posts] == [500, 100]
    assert posts[0].url.params["query"] == "INSERT INTO taas.job_events FORMAT JSONEachRow"
    assert posts[0].headers["authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_clickhouse_error_drops_without_raising() -> None:
    emitter = _emitter(lambda request: httpx.Response(500, text="Code: 60. Unknown table"))
    emitter.emit(_event())
    await emitter.flush()  # no exception
    assert emitter._dropped == 1


@pytest.mark.asyncio
async def test_full_queue_drops() -> None:
    posts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        posts.append(request)
        return httpx.Response(200)

    emitter = _emitter(handler, queue_max=2)
    for i in range(3):
        emitter.emit(_event(i))
    await emitter.flush()

    assert emitter._dropped == 1
    assert len(posts[0].content.splitlines()) == 2


@pytest.mark.asyncio
async def test_disabled_without_url_posts_nothing() -> None:
    posts: list[httpx.Request] = []
    emitter = _emitter(lambda request: posts.append(request) or httpx.Response(200), url="")
    await emitter.start()
    emitter.emit(_event())
    await emitter.aclose()
    assert posts == []


@pytest.mark.asyncio
async def test_background_task_posts_and_close_flushes() -> None:
    rows: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        rows.append(len(request.content.splitlines()))
        return httpx.Response(200)

    emitter = _emitter(handler)
    await emitter.start()
    for i in range(3):
        emitter.emit(_event(i))
    await emitter.aclose()  # cancels the loop, then flushes what it hadn't sent
    assert sum(rows) == 3
