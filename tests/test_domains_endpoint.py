"""Integration tests for GET /api/v1/domains.

Like the other DB-backed tests, these need a throwaway migrated Postgres via
``TEST_DATABASE_URL`` and skip otherwise.
"""

import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.routers.jobs import list_domains

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")


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
                "TRUNCATE backend_domains, domains, "
                "job_results, jobs, backends, users RESTART IDENTITY CASCADE"
            )
        )
        await s.commit()
        yield s
    await engine.dispose()


async def _backend(session, bid, *, enabled=True):
    await session.execute(
        text("INSERT INTO backends (id, url, enabled) VALUES (:i, :u, :e)"),
        {"i": bid, "u": f"http://e{bid}", "e": enabled},
    )


async def _domain(session, did, name, **meta):
    await session.execute(
        text(
            "INSERT INTO domains (id, name, label, legacy_engine_id, options_schema) "
            "VALUES (:i, :n, :l, :leg, CAST(:s AS jsonb))"
        ),
        {
            "i": did,
            "n": name,
            "l": meta.get("label"),
            "leg": meta.get("legacy_engine_id"),
            "s": meta.get("options_schema"),
        },
    )


async def _link(session, backend_id, domain_id, outputs):
    await session.execute(
        text("INSERT INTO backend_domains (backend_id, domain_id, outputs) VALUES (:b, :d, :o)"),
        {"b": backend_id, "d": domain_id, "o": outputs},
    )


@pytest.mark.asyncio
async def test_outputs_are_the_union_over_backends(session):
    await _backend(session, 1)
    await _backend(session, 2)
    await _domain(
        session,
        1,
        "default",
        label="Default",
        legacy_engine_id=1,
        options_schema='{"type": "object", "additionalProperties": false}',
    )
    await _link(session, 1, 1, ["alto", "txt"])
    await _link(session, 2, 1, ["alto", "json"])
    await session.commit()

    resp = await list_domains(username="alice", db=session)

    assert len(resp.domains) == 1
    entry = resp.domains[0]
    assert entry.name == "default"
    assert entry.outputs == ["alto", "json", "txt"]
    assert entry.label == "Default"
    assert entry.legacy_engine_id == 1
    assert entry.options_schema == {"type": "object", "additionalProperties": False}


@pytest.mark.asyncio
async def test_domain_of_disabled_backend_is_hidden(session):
    await _backend(session, 1)
    await _backend(session, 2, enabled=False)
    await _domain(session, 1, "default")
    await _domain(session, 2, "kramarky")
    await _link(session, 1, 1, ["alto", "txt"])
    await _link(session, 2, 2, ["alto", "txt"])
    # A disabled backend's outputs don't count for a domain an enabled one serves.
    await _link(session, 2, 1, ["geojson"])
    await session.commit()

    resp = await list_domains(username="alice", db=session)

    assert [d.name for d in resp.domains] == ["default"]
    assert resp.domains[0].outputs == ["alto", "txt"]
    assert resp.domains[0].options_schema is None


@pytest.mark.asyncio
async def test_unlinked_domain_is_hidden(session):
    await _domain(session, 1, "default")
    await session.commit()

    resp = await list_domains(username="alice", db=session)

    assert resp.domains == []
