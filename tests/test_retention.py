"""Tests for retention (step 6, D5): the pure cutoffs and eviction plan, and the TTL
sweep against a throwaway migrated Postgres (``TEST_DATABASE_URL``; skips otherwise)."""

import os
import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.schemas.runtime_config import RuntimeConfig
from app.services import retention
from app.services.retention import expired_job_cutoffs, plan_eviction

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")
NOW = datetime(2026, 10, 21, 12, 0, 0)

# ---- pure ----


def test_cutoffs() -> None:
    done, failed = expired_job_cutoffs(NOW, 86_400, 604_800)
    assert done == NOW - timedelta(days=1)
    assert failed == NOW - timedelta(days=7)


def test_nothing_evicted_within_the_cap() -> None:
    assert plan_eviction([("a", 10), ("b", 10)], used_bytes=100, cap_bytes=100) == []


def test_evicts_oldest_until_under_ninety_percent() -> None:
    rows = [("a", 10), ("b", 10), ("c", 10), ("d", 10)]
    # 120 used, cap 100 -> down to 90: a (110), b (100), c (90) go
    assert plan_eviction(rows, used_bytes=120, cap_bytes=100) == ["a", "b", "c"]


def test_eviction_stops_when_rows_run_out() -> None:
    assert plan_eviction([("a", 5)], used_bytes=1000, cap_bytes=100) == ["a"]


# ---- the TTL sweep (DB) ----


@pytest.fixture
async def session():
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
    async with factory() as s:
        await s.execute(
            text(
                "TRUNCATE batches, backend_domains, domains, job_results, jobs, backends, "
                "users, config RESTART IDENTITY CASCADE"
            )
        )
        await s.execute(text("INSERT INTO users (username, hashed_key) VALUES ('alice', 'x')"))
        await s.commit()
        yield s
    await engine.dispose()


async def _job(session, status: str, finished_days_ago: float | None) -> uuid.UUID:
    job_id = uuid.uuid4()
    finished = None if finished_days_ago is None else NOW - timedelta(days=finished_days_ago)
    await session.execute(
        text(
            "INSERT INTO jobs (id, username, external_id, status, finished_at) "
            "VALUES (:id, 'alice', :ext, :st, :fin)"
        ),
        {"id": job_id, "ext": f"ext-{uuid.uuid4()}", "st": status, "fin": finished},
    )
    await session.execute(
        text("INSERT INTO job_results (job_id, fmt) VALUES (:id, 'alto')"), {"id": job_id}
    )
    return job_id


@pytest.mark.asyncio
async def test_only_jobs_past_their_ttl_are_deleted(session, monkeypatch) -> None:
    monkeypatch.setattr(retention, "utcnow", lambda: NOW)
    deleted_objects: list[str] = []

    async def fake_delete(client, bucket, paths):
        deleted_objects.extend(paths)

    monkeypatch.setattr(retention, "delete_objects", fake_delete)
    done_old = await _job(session, "done", 2)
    await _job(session, "done", 0.5)
    await _job(session, "failed", 3)  # within the 7-day failed TTL
    await _job(session, "queued", None)
    await session.commit()

    assert await retention.delete_finished_jobs(session, None, "results") == 1

    left = (await session.execute(text("SELECT count(*) FROM jobs"))).scalar()
    assert left == 3
    gone = (
        await session.execute(text("SELECT count(*) FROM jobs WHERE id = :id"), {"id": done_old})
    ).scalar()
    assert gone == 0
    assert deleted_objects == [f"alice/{done_old}.xml.zst"]


def test_batch_ttl_depends_on_failures() -> None:
    finished = NOW - timedelta(days=2)
    assert retention.batch_expired(finished, False, NOW, 86_400, 604_800)
    assert not retention.batch_expired(finished, True, NOW, 86_400, 604_800)
    assert retention.batch_expired(NOW - timedelta(days=8), True, NOW, 86_400, 604_800)


@pytest.mark.asyncio
async def test_batch_pages_are_left_to_the_batch_sweep(session, monkeypatch) -> None:
    monkeypatch.setattr(retention, "utcnow", lambda: NOW)

    async def fake_delete(client, bucket, paths):
        pass

    monkeypatch.setattr(retention, "delete_objects", fake_delete)
    batch_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO batches (id, username, domain, outputs, status, total, finished_at) "
            "VALUES (:id, 'alice', 'default', '{alto}', 'finished', 1, :fin)"
        ),
        {"id": batch_id, "fin": NOW - timedelta(days=2)},
    )
    page = await _job(session, "done", 2)
    await session.execute(
        text("UPDATE jobs SET batch_id = :b WHERE id = :j"), {"b": batch_id, "j": page}
    )
    await session.commit()

    assert await retention.delete_finished_jobs(session, None, "results") == 0
    assert await retention.delete_expired_batches(session, None, "results") == 1
    assert (await session.execute(text("SELECT count(*) FROM jobs"))).scalar() == 0


async def _archived_batch(session, built_days_ago: float, size: int) -> uuid.UUID:
    batch_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO batches (id, username, domain, outputs, status, total, finished_at, "
            "archive_built_at, archive_size_bytes) "
            "VALUES (:id, 'alice', 'default', '{alto}', 'finished', 1, :at, :at, :size)"
        ),
        {"id": batch_id, "at": NOW - timedelta(days=built_days_ago), "size": size},
    )
    return batch_id


async def _stored_result(session, stored_days_ago: float, size: int) -> uuid.UUID:
    job_id = await _job(session, "done", stored_days_ago)
    await session.execute(
        text("UPDATE jobs SET stored_at = :at WHERE id = :id"),
        {"at": NOW - timedelta(days=stored_days_ago), "id": job_id},
    )
    await session.execute(
        text("UPDATE job_results SET size_bytes = :size WHERE job_id = :id"),
        {"size": size, "id": job_id},
    )
    return job_id


def _limits(monkeypatch, max_age_days: float, cap: int | None) -> list[str]:
    monkeypatch.setattr(retention, "utcnow", lambda: NOW)

    config = RuntimeConfig(
        results_max_age_seconds=int(max_age_days * 86_400), results_max_bytes=cap
    )
    monkeypatch.setattr(
        retention.config_service, "get_runtime_config", AsyncMock(return_value=config)
    )
    deleted: list[str] = []

    async def fake_delete(client, bucket, paths):
        deleted.extend(paths)

    monkeypatch.setattr(retention, "delete_objects", fake_delete)
    return deleted


async def _archive_built(session, batch_id) -> bool:
    query = text("SELECT archive_built_at IS NOT NULL FROM batches WHERE id = :id")
    return (await session.execute(query, {"id": batch_id})).scalar_one()


@pytest.mark.asyncio
async def test_over_the_cap_a_batch_archive_goes_before_page_results(session, monkeypatch) -> None:
    deleted = _limits(monkeypatch, max_age_days=30, cap=1000)
    batch_id = await _archived_batch(session, built_days_ago=0.1, size=600)
    job_id = await _stored_result(session, stored_days_ago=1, size=600)  # older than the archive
    await session.commit()

    assert await retention.evict_results(session, None, "results") == 1
    assert deleted == [f"alice/batches/{batch_id}.tar.zst"]
    assert not await _archive_built(session, batch_id)  # the next download rebuilds it
    evicted = text("SELECT evicted_at IS NOT NULL FROM job_results WHERE job_id = :id")
    assert not (await session.execute(evicted, {"id": job_id})).scalar_one()


@pytest.mark.asyncio
async def test_a_batch_archive_ages_out_like_results(session, monkeypatch) -> None:
    deleted = _limits(monkeypatch, max_age_days=1, cap=None)
    old = await _archived_batch(session, built_days_ago=2, size=10)
    new = await _archived_batch(session, built_days_ago=0.5, size=10)
    await session.commit()

    assert await retention.evict_results(session, None, "results") == 1
    assert deleted == [f"alice/batches/{old}.tar.zst"]
    assert await _archive_built(session, new)
