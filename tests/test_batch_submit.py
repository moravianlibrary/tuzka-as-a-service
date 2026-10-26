"""Pins what submitting a batch does: the batch and job rows, the uploaded pages, the
queue, and how a rejected archive is undone. Against a throwaway migrated Postgres
(``TEST_DATABASE_URL``) and Redis; skips otherwise. The uploads bucket is a fake."""

import asyncio
import io
import os
import tarfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import UploadFile
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import Settings
from app.exceptions import PayloadTooLarge, Unprocessable, UnsupportedMediaType
from app.models.batch import Batch
from app.models.job import Job
from app.routers import batches
from app.services.redis_jobs import peek_pending
from app.services.scopes import Principal
from app.vocabulary import BatchStatus, JobStatus

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
IMAGE = (Path(__file__).resolve().parents[1] / "test-data" / "sample.jpg").read_bytes()


class FakeIncoming:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_object(self, bucket: str, path: str, stream: Any, length: int, **_: Any) -> None:
        self.objects[path] = stream.read()

    async def remove_objects(self, bucket: str, objects: list[Any]) -> list[Any]:
        for obj in objects:
            self.objects.pop(obj._name, None)
        return []


@pytest.fixture
async def db():
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
    async with factory() as session:
        await session.execute(
            text(
                "TRUNCATE batches, backend_domains, domains, job_results, jobs, backends, users, "
                "config RESTART IDENTITY CASCADE"
            )
        )
        await session.execute(
            text("INSERT INTO users (username, hashed_key) VALUES ('alice', 'x')")
        )
        await session.execute(text("INSERT INTO backends (id, url) VALUES (1, 'http://e:8000')"))
        await session.execute(text("INSERT INTO domains (id, name) VALUES (1, 'default')"))
        await session.execute(
            text(
                "INSERT INTO backend_domains (backend_id, domain_id, outputs) "
                "VALUES (1, 1, '{alto,txt}')"
            )
        )
        await session.commit()
        yield session
    await engine.dispose()


@pytest.fixture
def incoming() -> FakeIncoming:
    return FakeIncoming()


def _tar(members: dict[str, bytes]) -> io.BytesIO:
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    data.seek(0)
    return data


async def _submit(db, r, incoming: FakeIncoming, archive: io.BytesIO, **settings: Any):
    request = SimpleNamespace(
        headers={}, app=SimpleNamespace(state=SimpleNamespace(incoming_client=incoming))
    )
    return await batches.submit_batch(
        request=request,  # type: ignore[arg-type]
        archive=UploadFile(file=archive, filename="pages.tar"),
        external_id="vol-1",
        domain=None,
        outputs="alto,txt",
        options=None,
        username="alice",
        principal=Principal("alice"),
        db=db,
        r=r,
        settings=Settings().model_copy(update=settings),
    )


async def _batches(db) -> list[Batch]:
    return list(await db.scalars(select(Batch)))


@pytest.mark.asyncio
async def test_every_image_becomes_a_queued_page_in_archive_order(
    db, redis_client, incoming
) -> None:
    archive = _tar({"b/p2.jpg": IMAGE, "README.txt": b"hi", "a/p1.jpg": IMAGE})

    response = await _submit(db, redis_client, incoming, archive)

    assert (response.external_id, response.total, response.skipped) == ("vol-1", 2, ["README.txt"])
    (batch,) = await _batches(db)
    assert (batch.id, batch.status, batch.total) == (response.batch_id, BatchStatus.QUEUED, 2)
    jobs = list(await db.scalars(select(Job).order_by(Job.source_name)))
    assert [(j.source_name, j.status, j.batch_id) for j in jobs] == [
        ("a/p1.jpg", JobStatus.QUEUED, batch.id),
        ("b/p2.jpg", JobStatus.QUEUED, batch.id),
    ]
    assert sorted(incoming.objects) == sorted(f"alice/{j.id}.jpg" for j in jobs)
    queued = [entry.job_id for entry in await peek_pending(redis_client, 10)]
    by_name = {j.source_name: str(j.id) for j in jobs}
    assert queued == [by_name["b/p2.jpg"], by_name["a/p1.jpg"]]  # archive order
    counts = await redis_client.hgetall(f"batch:{batch.id}")
    assert counts[b"total"] == b"2"


@pytest.mark.asyncio
async def test_an_archive_without_images_is_refused_and_undone(db, redis_client, incoming) -> None:
    archive = _tar({"README.txt": b"hi", "empty.jpg": b""})

    with pytest.raises(Unprocessable, match="No images"):
        await _submit(db, redis_client, incoming, archive)

    assert await _batches(db) == []
    assert await peek_pending(redis_client, 10) == []


@pytest.mark.asyncio
async def test_too_many_pages_undoes_the_pages_already_stored(db, redis_client, incoming) -> None:
    archive = _tar({"p1.jpg": IMAGE, "p2.jpg": IMAGE})

    with pytest.raises(PayloadTooLarge, match="More than 1 pages"):
        await _submit(db, redis_client, incoming, archive, batch_max_pages=1)

    assert await _batches(db) == []
    assert list(await db.scalars(select(Job))) == []
    assert incoming.objects == {}


@pytest.mark.asyncio
async def test_a_page_over_the_upload_limit_is_too_large(db, redis_client, incoming) -> None:
    archive = _tar({"p1.jpg": IMAGE})

    with pytest.raises(PayloadTooLarge):
        await _submit(db, redis_client, incoming, archive, max_upload_bytes=10)

    assert await _batches(db) == []


@pytest.mark.asyncio
async def test_a_file_that_is_no_archive_is_refused_before_a_batch_exists(
    db, redis_client, incoming
) -> None:
    with pytest.raises(UnsupportedMediaType):
        await _submit(db, redis_client, incoming, io.BytesIO(IMAGE))

    assert await _batches(db) == []


@pytest.mark.asyncio
async def test_a_session_that_expires_on_commit_submits_too(db, redis_client, incoming) -> None:
    # The steps work from the parsed request, not the committed batch row, which a
    # session like this one would make lazily reload (and fail, on an async session).
    expiring = async_sessionmaker(db.bind, expire_on_commit=True)
    archive = _tar({"p1.jpg": IMAGE})

    async with expiring() as session:
        response = await _submit(session, redis_client, incoming, archive)

    assert response.total == 1
    assert len(await peek_pending(redis_client, 10)) == 1


class SlowIncoming(FakeIncoming):
    """Takes a moment per upload, and records how many ran at once."""

    def __init__(self) -> None:
        super().__init__()
        self.running = 0
        self.most_at_once = 0

    async def put_object(self, bucket: str, path: str, stream: Any, length: int, **_: Any) -> None:
        self.running += 1
        self.most_at_once = max(self.most_at_once, self.running)
        await asyncio.sleep(0.01)
        await super().put_object(bucket, path, stream, length)
        self.running -= 1


@pytest.mark.asyncio
async def test_pages_upload_a_few_at_a_time(db, redis_client) -> None:
    incoming = SlowIncoming()
    archive = _tar({f"p{i:02}.jpg": IMAGE for i in range(20)})

    response = await _submit(db, redis_client, incoming, archive)

    assert response.total == 20 and len(incoming.objects) == 20
    assert incoming.most_at_once == batches.UPLOAD_CONCURRENCY


class FailingIncoming(FakeIncoming):
    """Stores the first page, then the storage goes away."""

    async def put_object(self, bucket: str, path: str, stream: Any, length: int, **_: Any) -> None:
        if self.objects:
            raise ConnectionError("storage down")
        await super().put_object(bucket, path, stream, length)


@pytest.mark.asyncio
async def test_a_storage_failure_undoes_the_batch_and_lets_the_error_through(
    db, redis_client
) -> None:
    incoming = FailingIncoming()
    archive = _tar({"p1.jpg": IMAGE, "p2.jpg": IMAGE})

    with pytest.raises(ConnectionError):
        await _submit(db, redis_client, incoming, archive)

    assert await _batches(db) == []
    assert incoming.objects == {}
