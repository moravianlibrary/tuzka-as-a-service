"""Integration tests for GET /api/v1/domains.

Like the other DB-backed tests, these need a throwaway migrated Postgres via
``TEST_DATABASE_URL`` and skip otherwise.
"""

import pytest
from sqlalchemy import text

from app.routers.jobs import list_domains
from app.services.scopes import Principal


@pytest.fixture
async def session(database):
    """A session on the emptied test database; the tests add their own users."""
    async with database() as s:
        yield s


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

    resp = await list_domains(username="alice", principal=Principal("alice"), db=session)

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

    resp = await list_domains(username="alice", principal=Principal("alice"), db=session)

    assert [d.name for d in resp.domains] == ["default"]
    assert resp.domains[0].outputs == ["alto", "txt"]
    assert resp.domains[0].options_schema is None


@pytest.mark.asyncio
async def test_unlinked_domain_is_hidden(session):
    await _domain(session, 1, "default")
    await session.commit()

    resp = await list_domains(username="alice", principal=Principal("alice"), db=session)

    assert resp.domains == []


@pytest.mark.asyncio
async def test_scoped_key_sees_only_its_domains_and_outputs(session):
    await _backend(session, 1)
    await _domain(session, 1, "default")
    await _domain(session, 2, "kramarky")
    await _link(session, 1, 1, ["alto", "txt"])
    await _link(session, 1, 2, ["alto", "txt"])
    await session.commit()

    key = Principal(
        "alice", allowed_domains=frozenset({"default"}), allowed_outputs=frozenset({"txt"})
    )
    resp = await list_domains(username="alice", principal=key, db=session)

    assert [(d.name, d.outputs) for d in resp.domains] == [("default", ["txt"])]
