"""Tests for domain sync: the pure ``plan_domain_links`` rules, and the DB side.

The DB tests need a throwaway migrated Postgres via ``TEST_DATABASE_URL`` and skip
otherwise; the pure ones always run.
"""

import logging
import os

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.engines.base import DomainInfo, EngineInfo
from app.services.domain_sync import (
    all_served_capabilities,
    apply_declared_capabilities,
    apply_domain_sync,
    plan_domain_links,
    served_capabilities,
)

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL")

ALTO_TXT = frozenset({"alto", "txt"})
SCHEMA_V1 = {"type": "object", "properties": {}, "additionalProperties": False}
SCHEMA_V2 = {
    "type": "object",
    "properties": {"alto_profile": {"enum": ["ndk", "basic"]}},
    "additionalProperties": False,
}


def _info(**domains: DomainInfo) -> EngineInfo:
    return EngineInfo(domains=domains, version="1.0")


# ---- plan_domain_links (pure) ----


def test_new_domain_stores_its_schema() -> None:
    links, store, changed = plan_domain_links(
        _info(default=DomainInfo(ALTO_TXT, SCHEMA_V1)), known_schemas={}
    )
    assert links == {"default": ALTO_TXT}
    assert store == {"default": SCHEMA_V1}
    assert changed == []


def test_equal_schema_links_without_change() -> None:
    links, store, changed = plan_domain_links(
        _info(default=DomainInfo(ALTO_TXT, SCHEMA_V1)), known_schemas={"default": SCHEMA_V1}
    )
    assert links == {"default": ALTO_TXT}
    assert store == {}
    assert changed == []


def test_different_schema_links_and_replaces() -> None:
    links, store, changed = plan_domain_links(
        _info(default=DomainInfo(ALTO_TXT, SCHEMA_V2)), known_schemas={"default": SCHEMA_V1}
    )
    assert links == {"default": ALTO_TXT}
    assert store == {"default": SCHEMA_V2}
    assert changed == ["default"]


def test_no_schema_never_clears_stored_one() -> None:
    links, store, changed = plan_domain_links(
        _info(default=DomainInfo(ALTO_TXT, None)), known_schemas={"default": SCHEMA_V1}
    )
    assert links == {"default": ALTO_TXT}
    assert store == {}
    assert changed == []


def test_known_domain_without_schema_gets_one_without_warning() -> None:
    _, store, changed = plan_domain_links(
        _info(kramarky=DomainInfo(ALTO_TXT, SCHEMA_V1)), known_schemas={"kramarky": None}
    )
    assert store == {"kramarky": SCHEMA_V1}
    assert changed == []


# ---- apply_domain_sync + served_capabilities (DB) ----

_SCHEMA_OF_DEFAULT = text("SELECT options_schema FROM domains WHERE name = 'default'")


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
                "TRUNCATE job_analytics, backend_domains, domains, engine_versions, "
                "job_results, jobs, backends, users RESTART IDENTITY CASCADE"
            )
        )
        await s.execute(text("INSERT INTO backends (id, url) VALUES (1, 'http://e1')"))
        await s.commit()
        yield s
    await engine.dispose()


@pytest.mark.asyncio
async def test_apply_links_outputs_and_stores_schema(session) -> None:
    info = _info(
        default=DomainInfo(ALTO_TXT, SCHEMA_V1),
        maps=DomainInfo(frozenset({"json", "geojson"}), SCHEMA_V1),
    )
    await apply_domain_sync(session, 1, info)
    await session.commit()

    assert await served_capabilities(session, 1) == {
        "default": ALTO_TXT,
        "maps": frozenset({"json", "geojson"}),
    }
    stored = await session.scalar(_SCHEMA_OF_DEFAULT)
    assert stored == SCHEMA_V1


@pytest.mark.asyncio
async def test_apply_rebuilds_links_and_newest_schema_wins(session, caplog) -> None:
    await apply_domain_sync(
        session,
        1,
        _info(default=DomainInfo(ALTO_TXT, SCHEMA_V1), kramarky=DomainInfo(ALTO_TXT, SCHEMA_V1)),
    )
    await session.commit()

    with caplog.at_level(logging.WARNING, logger="app.services.domain_sync"):
        info = _info(default=DomainInfo(frozenset({"alto"}), SCHEMA_V2))
        await apply_domain_sync(session, 1, info)
        await session.commit()

    # kramarky is no longer served by this backend, but its domain row stays.
    assert await served_capabilities(session, 1) == {"default": frozenset({"alto"})}
    stored = await session.scalar(_SCHEMA_OF_DEFAULT)
    assert stored == SCHEMA_V2
    assert "changed" in caplog.text


async def _sources(session) -> dict[str, tuple[str, list[str]]]:
    rows = await session.execute(
        text(
            "SELECT d.name, bd.source, bd.outputs FROM backend_domains bd "
            "JOIN domains d ON d.id = bd.domain_id WHERE bd.backend_id = 1"
        )
    )
    return {name: (source, sorted(outputs)) for name, source, outputs in rows.all()}


@pytest.mark.asyncio
async def test_empty_describe_keeps_remembered_rows(session) -> None:
    await apply_domain_sync(session, 1, _info(default=DomainInfo(ALTO_TXT, SCHEMA_V1)))
    await session.commit()

    await apply_domain_sync(session, 1, _info())  # engine down / scaled to zero
    await session.commit()

    assert await served_capabilities(session, 1) == {"default": ALTO_TXT}


@pytest.mark.asyncio
async def test_engine_sync_replaces_declared_rows(session) -> None:
    await apply_declared_capabilities(session, 1, {"default": ["alto", "txt"], "maps": ["json"]})
    await session.commit()
    assert await _sources(session) == {
        "default": ("declared", ["alto", "txt"]),
        "maps": ("declared", ["json"]),
    }

    await apply_domain_sync(session, 1, _info(default=DomainInfo(frozenset({"alto"}), None)))
    await session.commit()
    assert await _sources(session) == {"default": ("engine", ["alto"])}


@pytest.mark.asyncio
async def test_declared_never_overrides_engine_rows(session) -> None:
    await apply_domain_sync(session, 1, _info(default=DomainInfo(ALTO_TXT, None)))
    await session.commit()

    await apply_declared_capabilities(session, 1, {"default": ["geojson"], "maps": ["json"]})
    await session.commit()

    assert await _sources(session) == {
        "default": ("engine", ["alto", "txt"]),
        "maps": ("declared", ["json"]),
    }


@pytest.mark.asyncio
async def test_all_served_capabilities_in_one_query(session) -> None:
    await session.execute(text("INSERT INTO backends (id, url) VALUES (2, 'http://e2')"))
    await apply_domain_sync(session, 1, _info(default=DomainInfo(ALTO_TXT, None)))
    await apply_domain_sync(session, 2, _info(maps=DomainInfo(frozenset({"json"}), None)))
    await session.commit()

    assert await all_served_capabilities(session) == {
        1: {"default": ALTO_TXT},
        2: {"maps": frozenset({"json"})},
    }
