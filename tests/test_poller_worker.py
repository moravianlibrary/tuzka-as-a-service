"""Pins what the poller does with a pass's poll outcomes (``process_chunk``) and with a
finished job (``harvest``): the job row, its Redis state, the events. Against a
throwaway migrated Postgres (``TEST_DATABASE_URL``) and Redis; skips otherwise. The
engine and the object storage are fakes."""

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
from app.engines.base import EngineStatus, OutputUnavailable, Result
from app.models.job import Job, JobResult
from app.schemas.redis import EngineRun, JobState
from app.services.redis_jobs import (
    PENDING_HARVEST_KEY,
    enqueue_job,
    get_job,
    release_done,
    set_running,
)
from app.vocabulary import EngineState, FailureStage, JobEventName, JobStatus, PollStatus
from app.workers.poller.types import PollOutcome
from app.workers.poller.worker import PollerWorker
from tests.factories import job_state

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
TTL = 3600
BACKEND_ID = 1
ALTO = (
    b'<alto xmlns="http://www.loc.gov/standards/alto/ns-v3#"><Layout><Page><PrintSpace>'
    b'<TextBlock><TextLine><String CONTENT="hello"/></TextLine></TextBlock>'
    b"</PrintSpace></Page></Layout></alto>"
)
RUN = EngineRun(
    created_at="2026-10-26T10:00:00+00:00",
    started_at="2026-10-26T10:00:05+00:00",
    finished_at="2026-10-26T10:00:25+00:00",
    metrics={"n_lines": 1, "mean_conf": 0.9},
)


class FakeStorage:
    """Both results clients: keeps what was put, presigns a predictable URL."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_object(self, bucket: str, path: str, stream: Any, length: int, **_: Any) -> None:
        self.objects[path] = stream.read()

    async def presigned_get_object(self, bucket: str, path: str, **_: Any) -> str:
        return f"https://results/{path}"


class FakeEmitter:
    def __init__(self) -> None:
        self.events: list[Any] = []

    def emit(self, event: Any) -> None:
        self.events.append(event)


class FakeEngine:
    """An engine that finished every job; ``fetch`` answers per output or raises."""

    def __init__(self, outputs: dict[str, bytes | Exception]) -> None:
        self.outputs = outputs

    async def status(self, engine_job_id: str) -> EngineStatus:
        return EngineStatus(
            state=EngineState.DONE,
            created_at=RUN.created_at,
            started_at=RUN.started_at,
            finished_at=RUN.finished_at,
            metrics=RUN.metrics,
        )

    async def fetch(self, engine_job_id: str, output: str) -> Result:
        answer = self.outputs[output]
        if isinstance(answer, Exception):
            raise answer
        return Result(answer, "application/octet-stream")


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
            text("INSERT INTO backends (id, url) VALUES (:id, 'http://engine:8000')"),
            {"id": BACKEND_ID},
        )
        await db.commit()
    yield factory
    await engine.dispose()


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def emitter() -> FakeEmitter:
    return FakeEmitter()


@pytest.fixture
def worker(session_factory, redis_client, storage, emitter) -> PollerWorker:
    return PollerWorker(
        Settings(),
        redis_client,
        session_factory,
        httpx.AsyncClient(),
        storage,  # type: ignore[arg-type]
        storage,  # type: ignore[arg-type]
        emitter,  # type: ignore[arg-type]
        asyncio.Event(),
    )


async def _running_job(session_factory, r: aioredis.Redis) -> tuple[str, JobState]:
    """A job dispatched to the backend: its row, and its state as the poller reads it."""
    job_id = str(uuid.uuid4())
    async with session_factory() as db:
        db.add(
            Job(
                id=uuid.UUID(job_id),
                username="alice",
                status=JobStatus.RUNNING,
                domain="default",
                outputs=["alto", "txt"],
                engine_job_id="e-1",
                backend_id=BACKEND_ID,
                submitted_at=datetime(2026, 10, 26, 9, 59),
                dispatched_at=datetime(2026, 10, 26, 9, 59, 30),
            )
        )
        await db.commit()
    await enqueue_job(r, job_id, job_state(submitted_at=time.time()), TTL)
    await set_running(r, job_id, "e-1", "http://engine:8000", BACKEND_ID, TTL)
    state = await get_job(r, job_id)
    assert state is not None
    return job_id, state


async def _done_job(session_factory, r: aioredis.Redis) -> tuple[str, JobState]:
    """A job the engine finished: released for harvest, as the harvest pass reads it."""
    job_id, state = await _running_job(session_factory, r)
    await release_done(r, job_id, state, RUN)
    async with session_factory() as db:
        await db.execute(
            text("UPDATE jobs SET status = 'storing' WHERE id = :id"), {"id": uuid.UUID(job_id)}
        )
        await db.commit()
    harvested = await get_job(r, job_id)
    assert harvested is not None
    return job_id, harvested


def _outcome(job_id: str, state: JobState, status: PollStatus, **fields: Any) -> PollOutcome:
    base: dict[str, Any] = {
        "run": EngineRun(),
        "retryable": False,
        "http_status": None,
        "error": None,
    }
    return PollOutcome(job_id, status, state, **(base | fields))


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


# --- process_chunk ---


@pytest.mark.asyncio
async def test_a_done_poll_frees_the_slot_and_queues_the_harvest(
    worker, session_factory, redis_client
) -> None:
    job_id, state = await _running_job(session_factory, redis_client)

    await worker.process_chunk([_outcome(job_id, state, PollStatus.DONE, run=RUN)], 5, TTL)

    assert not await redis_client.sismember("jobs:inflight", job_id)
    assert await redis_client.zscore(PENDING_HARVEST_KEY, job_id) is not None
    assert (await _row(session_factory, job_id)).status == JobStatus.STORING
    assert worker.released_since_harvest == 1  # the next harvest pass takes at least it


@pytest.mark.asyncio
async def test_a_running_poll_backs_off_the_next_check(
    worker, session_factory, redis_client
) -> None:
    job_id, state = await _running_job(session_factory, redis_client)
    settings = Settings()

    await worker.process_chunk([_outcome(job_id, state, PollStatus.RUNNING)], 5, TTL)

    polled = await get_job(redis_client, job_id)
    assert polled is not None and polled.next_poll_at and polled.last_poll
    expected = min(2 * settings.poll_backoff_initial, settings.poll_backoff_max)
    assert polled.next_poll_at - polled.last_poll == pytest.approx(expected, abs=0.5)
    assert (await _row(session_factory, job_id)).status == JobStatus.RUNNING


@pytest.mark.asyncio
async def test_an_engine_failure_fails_the_job_with_its_attempt(
    worker, session_factory, redis_client, emitter
) -> None:
    job_id, state = await _running_job(session_factory, redis_client)
    outcome = _outcome(
        job_id,
        state,
        PollStatus.FAILED,
        http_status=422,
        error="bad image",
        stage=FailureStage.ENGINE,
    )

    published = await _published(redis_client, lambda: worker.process_chunk([outcome], 5, TTL))

    row = await _row(session_factory, job_id)
    assert (row.status, row.error) == (JobStatus.FAILED, "bad image")
    assert [(a.stage, a.http_status, a.retry) for a in row.attempts] == [
        (FailureStage.ENGINE, 422, False)
    ]
    failed = await get_job(redis_client, job_id)
    assert failed is not None and failed.status == JobStatus.FAILED
    assert not await redis_client.sismember("jobs:inflight", job_id)
    assert _event_names(emitter) == [JobEventName.FAILED]
    assert [(m["job_id"], m["status"], m["error"]) for m in published] == [
        (job_id, "failed", "bad image")
    ]


@pytest.mark.asyncio
async def test_a_retryable_failure_requeues_the_job_away_from_its_backend(
    worker, session_factory, redis_client, emitter
) -> None:
    job_id, state = await _running_job(session_factory, redis_client)
    outcome = _outcome(job_id, state, PollStatus.UNREACHABLE, retryable=True, error="refused")

    await worker.process_chunk([outcome], 5, TTL)

    row = await _row(session_factory, job_id)
    assert (row.status, row.engine_job_id, row.backend_id, row.dispatched_at, row.requeues) == (
        JobStatus.QUEUED,
        None,
        None,
        None,
        1,
    )
    assert [(a.stage, a.retry) for a in row.attempts] == [(FailureStage.POLL, True)]
    requeued = await get_job(redis_client, job_id)
    assert requeued is not None
    assert (requeued.status, requeued.requeues) == (JobStatus.QUEUED, 1)
    assert await redis_client.zscore("jobs:delayed", job_id) is not None  # after a backoff
    assert not await redis_client.sismember("jobs:inflight", job_id)
    assert _event_names(emitter) == [JobEventName.RETRIED]


@pytest.mark.asyncio
async def test_an_exhausted_requeue_budget_fails_the_job(
    worker, session_factory, redis_client
) -> None:
    job_id, state = await _running_job(session_factory, redis_client)
    spent = state.model_copy(update={"requeues": 5})
    outcome = _outcome(job_id, spent, PollStatus.UNREACHABLE, retryable=True)

    await worker.process_chunk([outcome], 5, TTL)

    row = await _row(session_factory, job_id)
    assert (row.status, row.error) == (
        JobStatus.FAILED,
        "engine unreachable, exceeded 5 requeue attempts",
    )


# --- a pass ---


@pytest.mark.asyncio
async def test_a_pass_polls_a_finished_job_and_the_next_one_harvests_it(
    worker, session_factory, redis_client, emitter
) -> None:
    job_id, _ = await _running_job(session_factory, redis_client)
    await redis_client.hset(f"job:{job_id}", "next_poll_at", "0")  # due now
    worker.adapter_for = _engine({"alto": ALTO, "txt": b"hello"})

    await worker.run_pass()
    assert worker.poll_task is not None
    await worker.poll_task  # the status checks run in the background
    assert (await _row(session_factory, job_id)).status == JobStatus.STORING
    wait = await worker.run_pass()

    assert (await _row(session_factory, job_id)).status == JobStatus.DONE
    assert _event_names(emitter) == [JobEventName.DONE]
    assert wait == Settings().poller_tick_seconds  # nothing left in flight or to harvest


# --- harvest ---


@pytest.mark.asyncio
async def test_a_harvest_stores_every_output_and_reports_the_job_done(
    worker, session_factory, redis_client, storage, emitter
) -> None:
    job_id, state = await _done_job(session_factory, redis_client)
    worker.adapter_for = _engine({"alto": ALTO, "txt": b"hello world\n"})

    published = await _published(redis_client, lambda: worker.harvest(job_id, state, None))

    assert sorted(storage.objects) == [f"alice/{job_id}.txt.zst", f"alice/{job_id}.xml.zst"]
    row = await _row(session_factory, job_id)
    assert row.status == JobStatus.DONE and row.stored_at is not None
    # The engine's own clock, from the status the job was released with.
    assert (row.engine_received_at, row.started_at, row.finished_at) == (
        datetime(2026, 10, 26, 10, 0, 0),
        datetime(2026, 10, 26, 10, 0, 5),
        datetime(2026, 10, 26, 10, 0, 25),
    )
    async with session_factory() as db:
        results = (await db.scalars(select(JobResult).where(JobResult.job_id == row.id))).all()
    assert {(r.fmt, r.presigned_url) for r in results} == {
        ("alto", f"https://results/alice/{job_id}.xml.zst"),
        ("txt", f"https://results/alice/{job_id}.txt.zst"),
    }
    done = await get_job(redis_client, job_id)
    assert done is not None and done.status == JobStatus.DONE
    assert await redis_client.zscore(PENDING_HARVEST_KEY, job_id) is None
    (event,) = emitter.events
    assert (event.event, event.lines, event.blocks, event.chars, event.mean_conf) == (
        JobEventName.DONE,
        1,
        1,
        10,  # non-whitespace characters of the txt output
        0.9,
    )
    assert event.run_s == 20.0
    assert [(m["status"], m["alto_url"], m["txt_url"]) for m in published] == [
        (
            "done",
            f"https://results/alice/{job_id}.xml.zst",
            f"https://results/alice/{job_id}.txt.zst",
        )
    ]
    assert await redis_client.hget("load:spp", "default") is not None  # /load's estimate


@pytest.mark.asyncio
async def test_a_second_harvest_reports_nothing_again(
    worker, session_factory, redis_client, emitter
) -> None:
    job_id, state = await _done_job(session_factory, redis_client)
    worker.adapter_for = _engine({"alto": ALTO, "txt": b"hello"})
    await worker.harvest(job_id, state, None)

    await worker.harvest(job_id, state, None)

    assert _event_names(emitter) == [JobEventName.DONE]
    assert (await _row(session_factory, job_id)).status == JobStatus.DONE


@pytest.mark.asyncio
async def test_an_output_the_engine_cannot_produce_fails_the_job(
    worker, session_factory, redis_client, storage, emitter
) -> None:
    job_id, state = await _done_job(session_factory, redis_client)
    worker.adapter_for = _engine(
        {"alto": ALTO, "txt": OutputUnavailable("txt", "georeferencing failed")}
    )

    await worker.harvest(job_id, state, None)

    row = await _row(session_factory, job_id)
    assert row.status == JobStatus.FAILED
    assert [(a.stage, a.http_status, a.retry) for a in row.attempts] == [
        (FailureStage.HARVEST, 409, False)
    ]
    assert row.finished_at == datetime(2026, 10, 26, 10, 0, 25)  # the engine's run is kept
    assert storage.objects == {}  # no partial results
    assert await redis_client.zscore(PENDING_HARVEST_KEY, job_id) is None
    assert _event_names(emitter) == [JobEventName.FAILED]


@pytest.mark.asyncio
async def test_a_transient_harvest_error_retries_the_harvest_later(
    worker, session_factory, redis_client, emitter
) -> None:
    job_id, state = await _done_job(session_factory, redis_client)
    worker.adapter_for = _engine({"alto": httpx.ConnectError("storage down"), "txt": b"x"})
    before = await redis_client.zscore(PENDING_HARVEST_KEY, job_id)

    await worker.harvest(job_id, state, None)

    after = await redis_client.zscore(PENDING_HARVEST_KEY, job_id)
    assert after is not None and before is not None and after > before
    assert (await _row(session_factory, job_id)).status == JobStatus.STORING
    assert emitter.events == []


@pytest.mark.asyncio
async def test_a_backend_lookup_that_cannot_reach_the_db_retries_the_harvest_later(
    worker, session_factory, redis_client, emitter
) -> None:
    job_id, state = await _done_job(session_factory, redis_client)

    async def adapter_for(state: JobState) -> FakeEngine:
        raise ConnectionRefusedError("database down")

    worker.adapter_for = adapter_for
    before = await redis_client.zscore(PENDING_HARVEST_KEY, job_id)

    await worker.harvest(job_id, state, None)

    after = await redis_client.zscore(PENDING_HARVEST_KEY, job_id)
    assert after is not None and before is not None and after > before  # not due again now
    assert (await _row(session_factory, job_id)).status == JobStatus.STORING
    assert emitter.events == []


@pytest.mark.asyncio
async def test_a_harvest_without_an_engine_job_id_fails_the_job(
    worker, session_factory, redis_client, emitter
) -> None:
    job_id, state = await _done_job(session_factory, redis_client)
    worker.adapter_for = _engine({"alto": ALTO, "txt": b"x"})

    await worker.harvest(job_id, state.model_copy(update={"engine_job_id": None}), None)

    assert (await _row(session_factory, job_id)).status == JobStatus.FAILED
    assert await redis_client.zscore(PENDING_HARVEST_KEY, job_id) is None  # not retried


@pytest.mark.asyncio
async def test_an_unexpected_harvest_error_fails_the_job(
    worker, session_factory, redis_client, emitter
) -> None:
    job_id, state = await _done_job(session_factory, redis_client)
    worker.adapter_for = _engine({"alto": ValueError("garbled"), "txt": b"x"})

    await worker.harvest(job_id, state, None)

    row = await _row(session_factory, job_id)
    assert (row.status, row.error, row.attempts) == (JobStatus.FAILED, "garbled", [])
    assert _event_names(emitter) == [JobEventName.FAILED]


def _engine(outputs: dict[str, bytes | Exception]):
    engine = FakeEngine(outputs)

    async def adapter_for(state: JobState) -> FakeEngine:
        return engine

    return adapter_for
