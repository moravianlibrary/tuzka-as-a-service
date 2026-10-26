"""Pins what the submit worker does with a claimed job (``dispatch_job``) and what a tick
dispatches: the job row, its Redis state, the events. Against a throwaway migrated
Postgres (``TEST_DATABASE_URL``) and Redis; skips otherwise. The engine and the object
storage are fakes."""

import asyncio
import json
import os
import time
import uuid
from datetime import datetime
from typing import Any

import httpx
import pytest
import redis.asyncio as aioredis
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.engines.base import EngineFullError
from app.models.backend import Backend
from app.models.job import Job
from app.schemas.redis import JobState
from app.services.redis_jobs import enqueue_job, get_job, peek_pending
from app.vocabulary import FailureStage, JobEventName, JobStatus
from app.workers.submit.worker import SubmitWorker
from tests.factories import job_state

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TTL = 3600
BACKEND_ID = 1
SERVED = {"default": frozenset({"alto", "txt"})}


class FakeObject:
    def __init__(self, data: bytes) -> None:
        self.data = data

    async def read(self) -> bytes:
        return self.data

    def close(self) -> None:
        pass

    async def release(self) -> None:
        pass


class FakeIncoming:
    """The uploads bucket: holds the given objects, raises for any other."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects

    async def get_object(self, bucket: str, path: str) -> FakeObject:
        if path not in self.objects:
            raise FileNotFoundError(path)
        return FakeObject(self.objects[path])


class FakeEmitter:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)


class FakeEngine:
    """A healthy engine that takes every job, or raises ``refuse`` on submit."""

    def __init__(self, refuse: Exception | None = None) -> None:
        self.refuse = refuse
        self.submitted: list[str] = []

    async def healthy(self) -> bool:
        return True

    async def submit(self, image: bytes, filename: str, *_: Any) -> str:
        if self.refuse is not None:
            raise self.refuse
        self.submitted.append(filename)
        return f"e-{len(self.submitted)}"

    async def version(self) -> str:
        return "1.2.3"


@pytest.fixture
async def session_factory():
    if not TEST_DATABASE_URL:
        pytest.skip("Set TEST_DATABASE_URL to a throwaway migrated DB to run these tests")
    engine = create_async_engine(TEST_DATABASE_URL)
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception:
        await engine.dispose()
        pytest.skip("TEST_DATABASE_URL not reachable")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        await db.execute(
            text(
                "TRUNCATE batches, backend_domains, domains, job_results, jobs, backends, users, "
                "config RESTART IDENTITY CASCADE"
            )
        )
        await db.execute(text("INSERT INTO users (username, hashed_key) VALUES ('alice', 'x')"))
        await db.execute(
            text("INSERT INTO backends (id, url, max_inflight) VALUES (:id, 'http://e:8000', 2)"),
            {"id": BACKEND_ID},
        )
        await db.execute(text("INSERT INTO domains (id, name) VALUES (1, 'default')"))
        await db.execute(
            text(
                "INSERT INTO backend_domains (backend_id, domain_id, outputs) "
                "VALUES (:id, 1, '{alto,txt}')"
            ),
            {"id": BACKEND_ID},
        )
        await db.commit()
    yield factory
    await engine.dispose()


@pytest.fixture
def incoming() -> FakeIncoming:
    return FakeIncoming({})


@pytest.fixture
def emitter() -> FakeEmitter:
    return FakeEmitter()


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def worker(session_factory, redis_client, incoming, emitter, engine) -> SubmitWorker:
    worker = SubmitWorker(
        Settings(),
        redis_client,
        session_factory,
        httpx.AsyncClient(),
        incoming,  # type: ignore[arg-type]
        emitter,  # type: ignore[arg-type]
        asyncio.Event(),
    )
    worker.adapters.for_backend = lambda backend, url: engine  # type: ignore[method-assign,assignment,return-value]
    return worker


async def _backend(session_factory) -> Backend:
    async with session_factory() as db:
        backend = await db.get(Backend, BACKEND_ID)
    assert backend is not None
    return backend


async def _queued_job(
    session_factory, r: aioredis.Redis, incoming: FakeIncoming, **fields: Any
) -> tuple[str, JobState]:
    """A submitted job with its upload in place: its row, its state, its queue entry."""
    job_id = str(uuid.uuid4())
    async with session_factory() as db:
        db.add(
            Job(
                id=uuid.UUID(job_id),
                username="alice",
                status=JobStatus.QUEUED,
                domain="default",
                outputs=["alto", "txt"],
                submitted_at=datetime(2026, 10, 26, 9, 59),
            )
        )
        await db.commit()
    await enqueue_job(r, job_id, job_state(submitted_at=time.time(), ext=".jpg", **fields), TTL)
    incoming.objects[f"alice/{job_id}.jpg"] = b"image"
    state = await get_job(r, job_id)
    assert state is not None
    return job_id, state


async def _row(session_factory, job_id: str) -> Job:
    async with session_factory() as db:
        job = await db.scalar(select(Job).where(Job.id == uuid.UUID(job_id)))
    assert job is not None
    return job


async def _published(r: aioredis.Redis, act) -> list[dict[str, Any]]:
    """What ``act`` published on alice's event channel."""
    pubsub = r.pubsub()
    await pubsub.subscribe("job:alice:events")
    await pubsub.get_message(timeout=1.0)  # the subscribe confirmation
    await act()
    messages = []
    while message := await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2):
        messages.append(json.loads(message["data"]))
    await pubsub.aclose()
    return messages


def _event_names(emitter: FakeEmitter) -> list[JobEventName]:
    return [event.event for event in emitter.events]


def _http_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://e:8000/api/v1/jobs")
    return httpx.HTTPStatusError("err", request=request, response=httpx.Response(code))


# --- dispatch_job ---


@pytest.mark.asyncio
async def test_a_dispatched_job_runs_on_the_backend(
    worker, session_factory, redis_client, incoming, emitter, engine
) -> None:
    job_id, state = await _queued_job(session_factory, redis_client, incoming)
    backend = await _backend(session_factory)

    await worker.dispatch_job(job_id, 100.0, backend, SERVED, state)

    assert engine.submitted == [f"{job_id}.jpg"]  # named by job id
    row = await _row(session_factory, job_id)
    assert (row.status, row.engine_job_id, row.engine_version, row.backend_id) == (
        JobStatus.RUNNING,
        "e-1",
        "1.2.3",
        BACKEND_ID,
    )
    assert row.dispatched_at is not None
    running = await get_job(redis_client, job_id)
    assert running is not None
    assert (running.status, running.engine_job_id, running.backend_id) == (
        JobStatus.RUNNING,
        "e-1",
        BACKEND_ID,
    )
    assert await redis_client.sismember("jobs:inflight", job_id)
    (event,) = emitter.events
    assert (event.event, event.engine_version) == (JobEventName.DISPATCHED, "1.2.3")


@pytest.mark.asyncio
async def test_a_job_the_backend_does_not_serve_goes_back_on_the_queue(
    worker, session_factory, redis_client, incoming, emitter, engine
) -> None:
    job_id, state = await _queued_job(session_factory, redis_client, incoming)
    backend = await _backend(session_factory)

    await worker.dispatch_job(job_id, 100.0, backend, {"maps": frozenset({"alto"})}, state)

    assert engine.submitted == []
    assert [e.job_id for e in await peek_pending(redis_client, 10)] == [job_id]
    assert (await _row(session_factory, job_id)).status == JobStatus.QUEUED
    assert emitter.events == []


@pytest.mark.asyncio
async def test_a_job_whose_upload_is_gone_fails(
    worker, session_factory, redis_client, incoming, emitter
) -> None:
    job_id, state = await _queued_job(session_factory, redis_client, incoming)
    incoming.objects.clear()
    backend = await _backend(session_factory)

    published = await _published(
        redis_client, lambda: worker.dispatch_job(job_id, 100.0, backend, SERVED, state)
    )

    row = await _row(session_factory, job_id)
    assert row.status == JobStatus.FAILED
    assert row.error is not None and row.error.startswith("Failed to read image: ")
    assert [(a.stage, a.backend_id, a.retry) for a in row.attempts] == [
        (FailureStage.DISPATCH, BACKEND_ID, False)
    ]
    failed = await get_job(redis_client, job_id)
    assert failed is not None and failed.status == JobStatus.FAILED
    assert _event_names(emitter) == [JobEventName.FAILED]
    assert [(m["job_id"], m["status"]) for m in published] == [(job_id, "failed")]


@pytest.mark.asyncio
async def test_a_full_engine_sends_the_job_back_on_the_queue(
    worker, session_factory, redis_client, incoming, emitter, engine
) -> None:
    job_id, state = await _queued_job(session_factory, redis_client, incoming)
    engine.refuse = EngineFullError()
    backend = await _backend(session_factory)

    await worker.dispatch_job(job_id, 100.0, backend, SERVED, state)

    assert [e.job_id for e in await peek_pending(redis_client, 10)] == [job_id]
    row = await _row(session_factory, job_id)
    assert (row.status, row.requeues, row.attempts) == (JobStatus.QUEUED, 0, [])
    assert emitter.events == []


@pytest.mark.asyncio
async def test_a_transient_dispatch_error_retries_the_job_elsewhere_later(
    worker, session_factory, redis_client, incoming, emitter, engine
) -> None:
    job_id, state = await _queued_job(session_factory, redis_client, incoming)
    engine.refuse = httpx.ConnectError("refused")
    backend = await _backend(session_factory)

    await worker.dispatch_job(job_id, 100.0, backend, SERVED, state)

    assert worker.health_cache[BACKEND_ID][0] is False  # sidelined for the cache window
    retried = await get_job(redis_client, job_id)
    assert retried is not None
    assert (retried.requeues, retried.avoid_backends) == (1, frozenset({BACKEND_ID}))
    assert await redis_client.zscore("jobs:delayed", job_id) is not None
    row = await _row(session_factory, job_id)
    assert (row.status, row.requeues) == (JobStatus.QUEUED, 1)
    assert [(a.stage, a.retry) for a in row.attempts] == [(FailureStage.DISPATCH, True)]
    assert _event_names(emitter) == [JobEventName.RETRIED]


@pytest.mark.asyncio
async def test_a_transient_dispatch_error_past_the_budget_fails_the_job(
    worker, session_factory, redis_client, incoming, emitter, engine
) -> None:
    job_id, state = await _queued_job(session_factory, redis_client, incoming, requeues=5)
    engine.refuse = _http_error(502)
    backend = await _backend(session_factory)

    await worker.dispatch_job(job_id, 100.0, backend, SERVED, state)

    row = await _row(session_factory, job_id)
    assert row.status == JobStatus.FAILED
    assert row.error is not None and row.error.endswith("(exceeded 5 requeue attempts)")
    assert [(a.http_status, a.retry) for a in row.attempts] == [(502, False)]
    assert _event_names(emitter) == [JobEventName.FAILED]


@pytest.mark.asyncio
async def test_a_rejected_job_fails_at_once(
    worker, session_factory, redis_client, incoming, emitter, engine
) -> None:
    job_id, state = await _queued_job(session_factory, redis_client, incoming)
    engine.refuse = _http_error(422)
    backend = await _backend(session_factory)

    await worker.dispatch_job(job_id, 100.0, backend, SERVED, state)

    row = await _row(session_factory, job_id)
    assert (row.status, row.requeues) == (JobStatus.FAILED, 0)
    assert [(a.stage, a.http_status, a.retry) for a in row.attempts] == [
        (FailureStage.DISPATCH, 422, False)
    ]
    failed = await get_job(redis_client, job_id)
    assert failed is not None and failed.status == JobStatus.FAILED
    assert _event_names(emitter) == [JobEventName.FAILED]


# --- a tick ---


@pytest.mark.asyncio
async def test_a_tick_dispatches_a_queued_job_to_a_healthy_backend(
    worker, session_factory, redis_client, incoming, emitter, engine
) -> None:
    job_id, _ = await _queued_job(session_factory, redis_client, incoming)

    await worker.run_tick()
    await asyncio.gather(*worker.sync_tasks)  # the domain sync the health check started

    assert engine.submitted == [f"{job_id}.jpg"]
    assert await peek_pending(redis_client, 10) == []
    assert (await _row(session_factory, job_id)).status == JobStatus.RUNNING
    assert _event_names(emitter) == [JobEventName.DISPATCHED]


@pytest.mark.asyncio
async def test_a_tick_dispatches_nothing_while_results_wait_to_be_stored(
    worker, session_factory, redis_client, incoming, engine
) -> None:
    job_id, _ = await _queued_job(session_factory, redis_client, incoming)
    worker.settings = Settings(unstored_job_max=1)
    await redis_client.zadd("jobs:pending_harvest", {"stored-later": 0})

    await worker.run_tick()
    await asyncio.gather(*worker.sync_tasks)

    assert engine.submitted == []
    assert [e.job_id for e in await peek_pending(redis_client, 10)] == [job_id]
    assert worker.paused_for_harvest
